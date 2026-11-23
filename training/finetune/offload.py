"""Experimental native offload: frozen CPU weights, GPU adapters, CPU AdamW.

This full-precision path is separate from NF4 and needs enough system RAM. The
output head stays resident to avoid repeated vocabulary transfers per loss chunk.
"""
from __future__ import annotations
import contextlib
import functools
import torch
from torch import nn
from torch.nn import functional as F


class _FrozenLinear(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, weight, bias):
        ctx.save_for_backward(weight); ctx.input_dtype = x.dtype
        return F.linear(x, weight.to(device=x.device, dtype=x.dtype, non_blocking=weight.is_pinned()),
                        bias.to(device=x.device, dtype=x.dtype) if bias is not None else None)

    @staticmethod
    def backward(ctx, gradient):
        weight, = ctx.saved_tensors
        value = gradient.matmul(weight.to(device=gradient.device, dtype=gradient.dtype, non_blocking=weight.is_pinned()))
        return value.to(ctx.input_dtype), None, None


class FrozenLinear(nn.Linear):
    def __init__(self, original, *, pin=False):
        nn.Module.__init__(self)
        self.in_features, self.out_features = original.in_features, original.out_features
        weight = original.weight.detach().cpu()
        if pin: weight = weight.pin_memory()
        self.weight = nn.Parameter(weight, requires_grad=False)
        self.bias = nn.Parameter(original.bias.detach().cpu(), requires_grad=False) if original.bias is not None else None

    def _apply(self, fn, recurse=True): return self

    def forward(self, x): return _FrozenLinear.apply(x, self.weight, self.bias)


class FrozenEmbedding(nn.Embedding):
    def __init__(self, original):
        nn.Module.__init__(self)
        self.weight = nn.Parameter(original.weight.detach().cpu(), requires_grad=False)
        self.padding_idx, self.num_embeddings, self.embedding_dim = original.padding_idx, original.num_embeddings, original.embedding_dim
        self.max_norm, self.norm_type, self.scale_grad_by_freq, self.sparse = original.max_norm, original.norm_type, original.scale_grad_by_freq, original.sparse

    def _apply(self, fn, recurse=True): return self

    def forward(self, indices):
        return F.embedding(indices.cpu(), self.weight, self.padding_idx, self.max_norm, self.norm_type,
                           self.scale_grad_by_freq, self.sparse).to(indices.device)


def enable(model, *, pin=False):
    if getattr(model, 'is_loaded_in_4bit', False):
        raise ValueError('CPU weight offload is a separate full-precision path; do not combine it with NF4.')
    output, embedding = model.get_output_embeddings(), model.get_input_embeddings()
    if output is not None and embedding is not None and output.weight is embedding.weight:
        output.weight = nn.Parameter(output.weight.detach().clone(), requires_grad=False)
    replaced = 0
    for parent in list(model.modules()):
        for name, child in list(parent.named_children()):
            if child is output: continue
            if isinstance(child, nn.Linear) and not any(p.requires_grad for p in child.parameters()):
                setattr(parent, name, FrozenLinear(child, pin=pin)); replaced += 1
            elif isinstance(child, nn.Embedding) and not child.weight.requires_grad:
                setattr(parent, name, FrozenEmbedding(child)); replaced += 1
    return {'mode': 'cpu_weight_offload', 'frozen_modules': replaced, 'output_head': 'resident', 'host_pinned': pin, 'hardware_validated': False}


def saved_activations(method):
    @functools.wraps(method)
    def call(self, *args, **kwargs):
        enabled = getattr(self.training, 'activation_offload', False) and str(self.device).startswith('cuda')
        with torch.autograd.graph.save_on_cpu(pin_memory=True) if enabled else contextlib.nullcontext():
            return method(self, *args, **kwargs)
    return call


class CPUAdamW(torch.optim.Optimizer):
    def __init__(self, params, **kwargs):
        super().__init__(params, kwargs)
        groups = [{**g, 'params': [nn.Parameter(p.detach().float().cpu().clone()) for p in g['params']]} for g in self.param_groups]
        self.cpu_optimizer = torch.optim.AdamW(groups, **kwargs)

    @torch.no_grad()
    def step(self, closure=None):
        loss = closure() if closure is not None else None
        for gpu, cpu in zip(self.param_groups, self.cpu_optimizer.param_groups):
            for key, value in gpu.items():
                if key != 'params': cpu[key] = value
            for parameter, shadow in zip(gpu['params'], cpu['params']):
                shadow.grad = parameter.grad.detach().float().cpu() if parameter.grad is not None else None
        self.cpu_optimizer.step()
        for gpu, cpu in zip(self.param_groups, self.cpu_optimizer.param_groups):
            for parameter, shadow in zip(gpu['params'], cpu['params']):
                parameter.copy_(shadow.to(device=parameter.device, dtype=parameter.dtype)); shadow.grad = None
        return loss

    def state_dict(self): return {'schema': 'cpu-adamw/1', 'cpu_optimizer': self.cpu_optimizer.state_dict()}

    def load_state_dict(self, value):
        if value.get('schema') != 'cpu-adamw/1': raise ValueError('Checkpoint optimizer differs from CPU AdamW')
        self.cpu_optimizer.load_state_dict(value['cpu_optimizer'])
        for gpu, cpu in zip(self.param_groups, self.cpu_optimizer.param_groups):
            with torch.no_grad():
                for parameter, shadow in zip(gpu['params'], cpu['params']):
                    shadow.copy_(parameter.detach().float().cpu())
            for key, value in cpu.items():
                if key != 'params': gpu[key] = value
