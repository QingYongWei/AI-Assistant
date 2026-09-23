import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
export const homeDir = () => process.env.PERSONZIT_HOME || path.join(os.homedir(), '.personzit');
export const paths = () => ({
  home: homeDir(), data: path.join(homeDir(), 'data'), runtime: path.join(homeDir(), 'runtime'),
  logs: path.join(homeDir(), 'logs'), workspaces: path.join(homeDir(), 'workspaces'),
  artifacts: path.join(homeDir(), 'artifacts'), cache: path.join(homeDir(), 'cache'),
  config: path.join(homeDir(), 'config.yaml'), db: path.join(homeDir(), 'data', 'personzit.db'),
  venv: path.join(homeDir(), '.venv')
});
