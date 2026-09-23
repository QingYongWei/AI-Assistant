import { Command } from 'commander';
import chalk from 'chalk';
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { paths, projectRoot } from './runtime/paths.js';
import { spawnService, stopServices, serviceStatus, logDirectory, logFile } from './runtime/process-manager.js';
import { request } from './api/client.js';
import readline from 'node:readline/promises';
import yaml from 'yaml';

const print = value => console.log(typeof value === 'string' ? value : JSON.stringify(value, null, 2));

async function init() {
  const p = paths();
  for (const key of ['data', 'runtime', 'logs', 'workspaces', 'artifacts', 'cache']) {
    fs.mkdirSync(p[key], { recursive: true });
  }
  if (!fs.existsSync(p.config)) {
    fs.copyFileSync(path.join(projectRoot, 'config', 'config.example.yaml'), p.config);
  }
  if (!fs.existsSync(p.venv)) {
    const created = spawnSync(process.env.PERSONZIT_PYTHON || 'python', ['-m', 'venv', p.venv], { stdio: 'inherit', windowsHide: true });
    if (created.status) throw new Error('Failed to create the Python virtual environment');
  }
  const python = process.platform === 'win32'
    ? path.join(p.venv, 'Scripts', 'python.exe')
    : path.join(p.venv, 'bin', 'python');
  const installed = spawnSync(python, ['-m', 'pip', 'install', '-e', path.join(projectRoot, 'python')], { stdio: 'inherit', windowsHide: true });
  if (installed.status) throw new Error('Failed to install Python dependencies');
  spawnSync(python, ['-c', 'from app.database import init_db; init_db()'], {
    cwd: path.join(projectRoot, 'python'),
    env: { ...process.env, PERSONZIT_HOME: p.home },
    stdio: 'inherit',
    windowsHide: true
  });
  console.log(chalk.green(`Initialization complete: ${p.home}`));
}

function detectVersion(command) {
  const result = spawnSync(command, ['--version'], {
    encoding: 'utf8',
    shell: process.platform === 'win32',
    windowsHide: true
  });
  return result.status === 0 ? (result.stdout || result.stderr).trim().split('\n')[0] : null;
}

async function configure() {
  const p = paths();
  fs.mkdirSync(path.dirname(p.config), { recursive: true });
  let cfg = {};
  if (fs.existsSync(p.config)) {
    try { cfg = yaml.parse(fs.readFileSync(p.config, 'utf8')) || {}; }
    catch { cfg = {}; }
  }

  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  const ask = async (question, defaultValue) => {
    const value = (await rl.question(`${question} `)).trim();
    return value === '' ? defaultValue : value;
  };
  const askBool = async (question, defaultValue) => {
    const value = (await ask(`${question} ${defaultValue ? '[Y/n]' : '[y/N]'}`, defaultValue ? 'y' : 'n')).toLowerCase();
    return ['y', 'yes', 'true', '1'].includes(value);
  };
  const askList = async (question, defaultValue) => {
    const value = await ask(question, defaultValue ?? '');
    return value ? value.split(/[,\n]/).map(item => item.trim()).filter(Boolean) : [];
  };

  console.log(chalk.cyan('\n=== PersonZit interactive configuration (Enter keeps the default/current value) ===\n'));
  console.log(chalk.bold('--- 1/4 Service ---'));
  cfg.server ??= {};
  cfg.server.host = await ask(`Listen address [${cfg.server.host ?? '127.0.0.1'}]:`, cfg.server.host ?? '127.0.0.1');
  cfg.server.port = Number(await ask(`Port [${cfg.server.port ?? 8765}]:`, cfg.server.port ?? 8765));

  console.log(chalk.bold('\n--- 2/4 Agents ---'));
  cfg.agents ??= { mock: { enabled: true } };
  cfg.agents.mock ??= { enabled: true };
  cfg.agents.default = await ask(`Default agent [${cfg.agents.default ?? 'mock'}] (mock/codex/claude):`, cfg.agents.default ?? 'mock');
  for (const name of ['codex', 'claude']) {
    const version = detectVersion(name);
    cfg.agents[name] ??= {};
    const label = version ? chalk.green(`detected: ${version}`) : chalk.yellow('not detected');
    const enabled = await askBool(`Enable ${name} (${label})?`, cfg.agents[name].enabled ?? false);
    cfg.agents[name].enabled = enabled;
    if (enabled) {
      cfg.agents[name].executable = await ask(`  executable [${cfg.agents[name].executable ?? name}]:`, cfg.agents[name].executable ?? name);
      cfg.agents[name].arguments ??= name === 'codex'
        ? ['exec', '--sandbox', 'workspace-write', '{prompt}']
        : ['-p', '{prompt}', '--permission-mode', 'acceptEdits'];
      cfg.agents[name].timeout_seconds = Number(await ask(`  timeout seconds [${cfg.agents[name].timeout_seconds ?? 1800}]:`, cfg.agents[name].timeout_seconds ?? 1800));
    }
  }

  console.log(chalk.bold('\n--- 3/4 Planner ---'));
  cfg.planner ??= {};
  cfg.planner.type = await ask(`Planner type [${cfg.planner.type ?? 'ai'}] (mock/ai):`, cfg.planner.type ?? 'ai');
  cfg.planner.agent = await ask(`Planner agent [${cfg.planner.agent ?? 'claude'}] (claude/codex):`, cfg.planner.agent ?? 'claude');
  cfg.planner.timeout_seconds = Number(await ask(`Planner timeout seconds [${cfg.planner.timeout_seconds ?? 300}]:`, cfg.planner.timeout_seconds ?? 300));

  console.log(chalk.bold('\n--- 4/4 DingTalk ---'));
  cfg.dingtalk ??= {};
  cfg.dingtalk.notify ??= { approval: true, clarification: true, completed: true, waiting_human: true, failed: true };
  cfg.dingtalk.enabled = await askBool('Enable DingTalk integration?', cfg.dingtalk.enabled ?? false);
  if (cfg.dingtalk.enabled) {
    cfg.dingtalk.webhook = await ask(`Group custom-robot webhook [${cfg.dingtalk.webhook ?? ''}]:`, cfg.dingtalk.webhook ?? '');
    cfg.dingtalk.secret = await ask(`Group custom-robot sign secret [${cfg.dingtalk.secret ?? ''}]:`, cfg.dingtalk.secret ?? '');
    cfg.dingtalk.client_id = await ask(`Open-platform AppKey/client_id [${cfg.dingtalk.client_id ?? ''}]:`, cfg.dingtalk.client_id ?? '');
    cfg.dingtalk.client_secret = await ask(`Open-platform AppSecret/client_secret [${cfg.dingtalk.client_secret ?? ''}]:`, cfg.dingtalk.client_secret ?? '');
    cfg.dingtalk.allowed_users = await askList(`Allowed user IDs [${(cfg.dingtalk.allowed_users ?? []).join(',')}]:`, (cfg.dingtalk.allowed_users ?? []).join(','));
    cfg.dingtalk.allowed_conversations = await askList(`Allowed conversation IDs [${(cfg.dingtalk.allowed_conversations ?? []).join(',')}]:`, (cfg.dingtalk.allowed_conversations ?? []).join(','));
    cfg.dingtalk.default_project = await ask(`Default project path [${cfg.dingtalk.default_project ?? ''}]:`, cfg.dingtalk.default_project ?? '');
    if (cfg.dingtalk.webhook) {
      const sendTest = await askBool('Send a test message to the group now?', false);
      if (sendTest) {
        try {
          const result = await request('/api/dingtalk/test', { method: 'POST', body: JSON.stringify({ message: 'PersonZit configuration test' }) });
          console.log(result.sent ? chalk.green('Test message sent') : chalk.yellow(`Send failed: ${result.detail}`));
        } catch (error) {
          console.log(chalk.yellow(`Send failed: ${error.message}`));
        }
      }
    } else if (!cfg.dingtalk.client_id) {
      console.log(chalk.yellow('Both webhook and client_id are empty; DingTalk integration will remain inactive.'));
    }
  }
  rl.close();
  fs.writeFileSync(p.config, yaml.stringify(cfg), 'utf8');
  console.log(chalk.green(`\nConfiguration saved: ${p.config}`));
  console.log('Run personzit restart if service settings changed.');
}

export async function run() {
  const cli = new Command().name('personzit').description('PersonZit local AI orchestration CLI').version('0.1.0');

  cli.command('init').description('Initialize the runtime environment').action(async () => {
    await init();
    console.log(chalk.cyan('Tip: run personzit configure to configure agents, planner, and DingTalk.'));
  });
  cli.command('configure').description('Run interactive configuration').action(configure);

  cli.command('start')
    .option('--foreground', 'Run API in the foreground; Worker always runs in the background')
    .action(async options => {
      spawnService('app.worker');
      if (options.foreground) print('Worker started in the background; API is starting in the foreground');
      spawnService('app.main', Boolean(options.foreground));
      if (!options.foreground) print('API and Worker started in the background');
    });

  cli.command('stop').description('Stop API and Worker').action(() => print(`Stopped ${stopServices()} service(s)`));
  cli.command('restart').description('Restart API and Worker').action(() => {
    stopServices();
    spawnService('app.main');
    spawnService('app.worker');
    print('Services restarted');
  });
  cli.command('status').description('Show service status').action(() => print(serviceStatus()));
  cli.command('doctor').description('Show runtime diagnostics').action(() => print({
    node: process.version,
    python: spawnSync('python', ['--version'], { encoding: 'utf8', windowsHide: true }).stdout.trim(),
    git: spawnSync('git', ['--version'], { encoding: 'utf8', windowsHide: true }).stdout.trim(),
    home: paths().home,
    services: serviceStatus()
  }));

  const task = cli.command('task');
  task.command('create')
    .requiredOption('--title <title>')
    .requiredOption('--description <description>')
    .requiredOption('--project <path>')
    .option('--priority <n>', 'Task priority', '0')
    .option('--verify <command>', 'Verification command; repeatable', (value, previous = []) => [...previous, value], [])
    .action(async options => print(await request('/api/tasks', {
      method: 'POST',
      body: JSON.stringify({
        title: options.title,
        description: options.description,
        project_path: path.resolve(options.project),
        priority: Number(options.priority),
        verification_commands: options.verify
      })
    })));
  task.command('list').action(async () => print(await request('/api/tasks')));
  task.command('show <id>').action(async id => print(await request(`/api/tasks/${id}`)));
  task.command('runs <id>').description('Show agent runs').action(async id => print(await request(`/api/tasks/${id}/runs`)));
  task.command('events <id>').action(async id => print(await request(`/api/tasks/${id}/events`)));
  task.command('clarify <id>')
    .requiredOption('--answer <answer>')
    .option('--answers <answer>', 'Backward-compatible alias for --answer')
    .action(async (id, options) => print(await request(`/api/tasks/${id}/clarify`, {
      method: 'POST',
      body: JSON.stringify({ answers: options.answer ?? options.answers })
    })));
  for (const action of ['approve', 'reject', 'pause', 'resume', 'cancel', 'retry']) {
    task.command(`${action} <id>`)
      .option('--reason <reason>')
      .action(async (id, options) => print(await request(`/api/tasks/${id}/${action}`, {
        method: 'POST',
        body: JSON.stringify({ reason: options.reason })
      })));
  }

  cli.command('run <requirement>')
    .option('--project <path>', 'Project path', process.cwd())
    .option('--verify <command>', 'Verification command; repeatable', (value, previous = []) => [...previous, value], [])
    .action(async (requirement, options) => print(await request('/api/tasks', {
      method: 'POST',
      body: JSON.stringify({
        title: requirement.slice(0, 80),
        description: requirement,
        project_path: path.resolve(options.project),
        priority: 0,
        verification_commands: options.verify
      })
    })));

  const dingtalk = cli.command('dingtalk');
  dingtalk.command('test')
    .option('--message <msg>', 'Test message', 'PersonZit DingTalk notification test')
    .action(async options => print(await request('/api/dingtalk/test', {
      method: 'POST',
      body: JSON.stringify({ message: options.message })
    })));
  dingtalk.command('status').action(async () => print(await request('/api/dingtalk/status')));

  const agent = cli.command('agent');
  agent.command('list').action(async () => print(await request('/api/agents')));
  agent.command('detect').action(async () => print(await request('/api/agents/detect', { method: 'POST' })));

  cli.command('logs')
    .option('--service <name>', 'api, worker, or dingtalk', 'api')
    .option('--lines <n>', 'Number of initial lines', '80')
    .option('--follow', 'Follow log output until Ctrl+C')
    .action(options => {
      const service = ['api', 'worker', 'dingtalk'].includes(options.service) ? options.service : 'api';
      const directory = logDirectory();
      const currentFile = () => {
        const exact = logFile(service);
        if (fs.existsSync(exact)) return exact;
        return fs.readdirSync(directory, { withFileTypes: true })
          .filter(entry => entry.isFile() && entry.name.startsWith(`${service}-`) && entry.name.endsWith('.log'))
          .map(entry => path.join(directory, entry.name))
          .sort()
          .pop();
      };

      let file = currentFile();
      if (!file) return print('No log file yet');
      const history = fs.readFileSync(file, 'utf8').split(/\r?\n/).filter(Boolean);
      const count = Math.max(1, Number(options.lines) || 80);
      print(`Log file: ${file}`);
      print(history.slice(-count).join('\n'));
      if (!options.follow) return undefined;

      let position = fs.statSync(file).size;
      process.stdout.write('--- following logs (Ctrl+C to exit) ---\n');
      const timer = setInterval(() => {
        const latest = currentFile();
        if (latest !== file && fs.existsSync(latest)) {
          file = latest;
          position = 0;
          process.stdout.write(`--- switched to ${file} ---\n`);
        }
        const stat = fs.statSync(file);
        if (stat.size < position) position = 0;
        if (stat.size === position) return;
        const handle = fs.openSync(file, 'r');
        const length = stat.size - position;
        const buffer = Buffer.alloc(length);
        fs.readSync(handle, buffer, 0, length, position);
        fs.closeSync(handle);
        process.stdout.write(buffer.toString('utf8'));
        position = stat.size;
      }, 1000);
      const stop = () => {
        clearInterval(timer);
        process.exit(0);
      };
      process.on('SIGINT', stop);
      process.on('SIGTERM', stop);
      return new Promise(() => {});
    });

  await cli.parseAsync();
}
