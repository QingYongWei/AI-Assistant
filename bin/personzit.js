#!/usr/bin/env node
import('../src/cli.js').then(({ run }) => run()).catch((error) => {
  console.error(error?.message ?? error);
  process.exitCode = 1;
});
