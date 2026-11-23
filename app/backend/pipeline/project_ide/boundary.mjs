// App-owned native file-tool boundary. Shell execution remains trusted local execution.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const excluded = new Set(['.git', '.hg', '.svn', '.venv', 'venv', 'env', 'node_modules', '__pycache__',
  '.pytest_cache', '.mypy_cache', '.ruff_cache', '.next', '.cache', '.tmp', '.superpowers', 'graphify-out',
  'vendor', 'dist', 'build', 'coverage', '.ssh', '.aws', '.azure', '.gnupg', '.npmrc', '.pypirc', '.netrc',
  'credentials', 'credentials.json', 'id_rsa', 'id_ed25519']);
const posix = value => value.replaceAll('\\', '/');
const key = value => process.platform === 'win32' ? value.toLowerCase() : value;

export default async function ProjectsBoundary({directory}) {
  const root = fs.realpathSync(directory);
  const permissions = process.env.PROJECTS_IDE_PERMISSION_DIR;
  async function commandApproval(input, args) {
    if (!permissions) throw new Error('The application command approval channel is unavailable.');
    if (typeof args.command !== 'string' || args.command.length > 60000) throw new Error('Invalid command length.');
    fs.mkdirSync(permissions, {recursive: true});
    const id = 'appcmd_' + crypto.randomBytes(16).toString('hex');
    const request = path.join(permissions, id + '.json');
    const reply = path.join(permissions, id + '.reply');
    const data = {id, sessionID: input.sessionID, permission: 'bash', patterns: [args.command],
      metadata: {workdir: args.workdir, description: args.description}, source: 'application_command'};
    fs.writeFileSync(request + '.tmp', JSON.stringify(data), {mode: 0o600});
    fs.renameSync(request + '.tmp', request);
    try {
      const deadline = Date.now() + 30 * 60 * 1000;
      while (Date.now() < deadline) {
        if (fs.existsSync(reply)) {
          const decision = JSON.parse(fs.readFileSync(reply, 'utf8'));
          if (decision.reply === 'once') return;
          throw new Error('The application rejected or cancelled this command. Do not bypass the denial.');
        }
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      throw new Error('Command approval expired; no command was executed.');
    } finally {
      fs.rmSync(request, {force: true}); fs.rmSync(reply, {force: true});
    }
  }
  function inside(candidate) {
    const relative = path.relative(key(root), key(candidate));
    return relative === '' || (!path.isAbsolute(relative) && relative !== '..' && !relative.startsWith('..' + path.sep));
  }
  function bound(value = '.') {
    if (typeof value !== 'string' || /[\x00-\x1f]/.test(value) || value.replace(/^[A-Za-z]:/, '').includes(':')) {
      throw new Error('Invalid project path. Use a complete forward-slash path.');
    }
    const candidate = path.resolve(root, value);
    if (!inside(candidate)) throw new Error('File access is outside the active workspace: ' + posix(root));
    const parts = path.relative(root, candidate).split(path.sep).map(part => part.toLowerCase());
    if (parts.some(part => excluded.has(part) || part.startsWith('.env') || /\.(pem|key|pfx|p12)$/.test(part))) {
      throw new Error('Credential stores, dependency environments, and generated files are excluded from managed context.');
    }
    let existing = candidate;
    while (!fs.existsSync(existing) && existing !== path.dirname(existing)) existing = path.dirname(existing);
    for (let cursor = existing; cursor !== root && cursor !== path.dirname(cursor); cursor = path.dirname(cursor)) {
      if (fs.lstatSync(cursor).isSymbolicLink()) throw new Error('Links and junctions are excluded from managed file context.');
    }
    if (!inside(fs.realpathSync(existing))) throw new Error('A project link escapes the active workspace.');
    return posix(candidate);
  }
  return {
    'tool.execute.before': async (input, output) => {
      const args = output.args;
      if (['read', 'write', 'edit'].includes(input.tool)) {
        args.filePath = bound(args.filePath);
      } else if (['glob', 'grep', 'list'].includes(input.tool)) {
        args.path = bound(args.path || '.');
        if (input.tool === 'glob' && (path.isAbsolute(args.pattern) || posix(args.pattern).split('/').includes('..'))) {
          throw new Error('Search patterns must stay inside the active workspace.');
        }
      } else if (input.tool === 'bash') {
        args.workdir = bound(args.workdir || '.');
        await commandApproval(input, args);
      } else if (input.tool === 'apply_patch') {
        const patch = args.patchText || args.patch || '';
        for (const line of patch.split('\n')) {
          const target = /^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$/.exec(line);
          if (target) bound(target[1]);
        }
      }
    },
    'tool.execute.after': async (input, output) => {
      if (input.tool === 'glob' && typeof output.output === 'string') {
        output.output = posix(output.output);
      }
    },
  };
}
