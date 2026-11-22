import {render, screen} from '@testing-library/react';
import {expect, it} from 'vitest';
import {TrialReport} from './TrialReport';

it('labels the separate experimental TPU quantization recipe', () => {
  render(<TrialReport trial={{import_id: 'tpu', mode: 'trial', state: 'partial', total_budget_minutes: 30,
    experimental: true, software_test: false, counts_as_full_training: false, security_evaluation: false,
    config: {runtime_recipe: {backend: 'tpu', recipe_id: 'experimental_tpu_symmetric_int4_bf16_v1'}}, reports: {}}} />);
  expect(screen.getByText(/Experimental TPU recipe: symmetric INT4 with BF16/)).toBeInTheDocument();
  expect(screen.getByText(/separate from CUDA NF4/)).toBeInTheDocument();
});

it('shows partial trial progress and descriptive paired losses without promoting a result', () => {
  render(<TrialReport trial={{import_id: 'one', mode: 'trial', state: 'partial', total_budget_minutes: 30,
    experimental: true, software_test: false, counts_as_full_training: false, security_evaluation: false,
    config: {phase: 'comparison', steps_done: 3, planned_steps: 8}, reports: {
      resources: {backend: 'cuda', hardware: {name: 'Remote GPU'}},
      comparison: {baseline: {token_mean_loss: 1.4}, tuned: {token_mean_loss: 1.2},
        loss_delta_tuned_minus_base: -.2, examples: [{view_id: 'view-a', task: 'change_analysis',
          base: {text: 'base response', state: 'finished'}, tuned: {text: 'tuned response', state: 'partial'}}]},
    }}} />);
  expect(screen.getByRole('heading', {name: /partial trial/i})).toBeInTheDocument();
  expect(screen.getByText(/3 of 8 steps/)).toBeInTheDocument();
  expect(screen.getByText(/does not count as full training or security evaluation/)).toBeInTheDocument();
  expect(screen.getByText('1.4000')).toBeInTheDocument();
  expect(screen.getByText('1.2000')).toBeInTheDocument();
  expect(screen.getByText('base response')).toBeInTheDocument();
  expect(screen.getByText('tuned response')).toBeInTheDocument();
  expect(screen.getByText(/remote resource report/i)).toBeInTheDocument();
});
