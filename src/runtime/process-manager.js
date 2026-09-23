import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import yaml from 'yaml';
import { Transform } from 'node:stream';
import { spawnSync } from 'node:child_process';
import { projectRoot, paths } from './paths.js';

const py = () => process.env.PERSONZIT_PYTHON || (process.platform === 'win32'
  ? path.join(paths().venv, 'Scripts', 'python.exe') : path.join(paths().venv, 'bin', 'python'));

// Python 3.14 的 venv python.exe 是 venvlauncher 蹦床：它会把基础解释器作为子进程再拉起一次，
// 且不透传"无控制台"创建参数。后台（detached，无控制台）启动时，Windows 会为内层进程分配
// 新控制台，弹出的两个空终端窗口一旦被用户关闭就会杀掉真正的 api/worker。
// pythonw 属于 GUI 子系统、永不分配控制台，stdout/stderr 管道与退出码仍正常穿透蹦床。
const backgroundPython = () => {
  if (process.env.PERSONZIT_PYTHON || process.platform !== 'win32') return py();
  const pythonw = path.join(paths().venv, 'Scripts', 'pythonw.exe');
  return fs.existsSync(pythonw) ? pythonw : py();
};

const serviceName = module => module.endsWith('worker') ? 'worker' : 'api';
const readConfig = () => {
  try { return yaml.parse(fs.readFileSync(paths().config, 'utf8')) || {}; } catch { return {}; }
};
export const logDirectory = () => {
  const configured = process.env.PERSONZIT_LOG_DIR || readConfig()?.logging?.directory || 'logs';
  return path.isAbsolute(configured) ? configured : path.join(paths().home, configured);
};
export const logFile = (service, date = new Date()) =>
  path.join(logDirectory(), `${service}-${date.toISOString().slice(0, 10).replace(/-/g, '')}.log`);


const timestamp = () => new Date().toISOString().replace('T', ' ').replace('Z', '');
const createLogTransform = service => {
  let pending = '';
  return new Transform({
    transform(chunk, encoding, callback) {
      try {
        pending += chunk.toString('utf8');
        const lines = pending.split(/\r?\n/);
        pending = lines.pop() ?? '';
        callback(null, lines.map(line => `${timestamp()} | ${service} | ${line}\n`).join(''));
      } catch (error) { callback(error); }
    },
    flush(callback) {
      if (pending) this.push(`${timestamp()} | ${service} | ${pending}\n`);
      pending = ''; callback();
    }
  });
};
const writeLog = (service, stream) => {
  let file = logFile(service);
  let fd = fs.openSync(file, 'a');
  stream.on('data', chunk => {
    try {
      const current = logFile(service);
      if (current !== file) {
        fs.closeSync(fd); file = current; fd = fs.openSync(file, 'a');
      }
      fs.writeSync(fd, chunk);
    } catch {}
  });
  stream.on('error', () => {});
};

export const pidFile = service => path.join(paths().runtime, `${service}.pid.json`);
export const processAlive = pid => {
  if (!pid || Number(pid) <= 0) return false;
  try { process.kill(Number(pid), 0); return true; } catch (error) {
    return error?.code === 'EPERM';
  }
};
export const serviceRunning = service => {
  const file = pidFile(service);
  if (!fs.existsSync(file)) return false;
  try {
    const { pid } = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (!processAlive(pid)) return false;
    return { pid: Number(pid) };
  } catch { return false; }
};

export function spawnService(module, foreground = false) {
  const service = serviceName(module);
  const existing = serviceRunning(service);
  if (existing) return { pid: existing.pid, alreadyRunning: true };

  const directory = logDirectory();
  fs.mkdirSync(directory, { recursive: true });
  fs.mkdirSync(paths().runtime, { recursive: true });
  const file = logFile(service);
  const child = spawn(foreground ? py() : backgroundPython(), ['-m', module], {
    cwd: path.join(projectRoot, 'python'),
    detached: !foreground,
    stdio: foreground ? 'inherit' : ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
    env: { ...process.env, PERSONZIT_HOME: paths().home, PERSONZIT_LOG_DIR: directory }
  });
  if (!foreground) {
    writeLog(service, child.stdout);
    writeLog(service, child.stderr);
    child.stdout?.unref?.();
    child.stderr?.unref?.();
    child.unref();
  }
  fs.writeFileSync(pidFile(service), JSON.stringify({ pid: child.pid, startedAt: new Date().toISOString(), logFile: file }, null, 2));
  return child;
}

export function stopServices() {
  let count = 0;
  for (const service of ['api', 'worker']) {
    const file = pidFile(service);
    if (!fs.existsSync(file)) continue;
    try {
      const { pid } = JSON.parse(fs.readFileSync(file, 'utf8'));
      if (processAlive(pid)) {
        if (process.platform === 'win32') spawnSync('taskkill', ['/T', '/F', '/PID', String(pid)], { stdio: 'ignore', windowsHide: true });
        else process.kill(Number(pid));
        count++;
      }
    } catch {} finally {
      fs.rmSync(file, { force: true });
    }
  }
  return count;
}

export function serviceStatus() {
  return ['api', 'worker'].map(service => {
    const file = pidFile(service);
    if (!fs.existsSync(file)) return { name: service, running: false, logFile: logFile(service) };
    try {
      const info = JSON.parse(fs.readFileSync(file, 'utf8'));
      if (!processAlive(info.pid)) return { name: service, running: false, stalePid: info.pid, logFile: logFile(service) };
      return { name: service, running: true, ...info, logFile: info.logFile || logFile(service) };
    } catch {
      return { name: service, running: false, logFile: logFile(service) };
    }
  });
}
