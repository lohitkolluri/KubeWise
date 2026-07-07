#!/usr/bin/env node
'use strict';

const { spawnSync } = require('child_process');
const path = require('path');

const launcher = path.join(
  path.dirname(require.resolve('kwctl/package.json')),
  'bin',
  'kwctl.js'
);

const result = spawnSync(process.execPath, [launcher, ...process.argv.slice(2)], {
  stdio: 'inherit',
});

process.exit(result.status === null ? 1 : result.status);
