#!/usr/bin/env node
'use strict';

const { spawnSync } = require('child_process');

const PLATFORM_PACKAGES = {
  'darwin-arm64': 'kwctl-darwin-arm64',
  'darwin-x64': 'kwctl-darwin-amd64',
  'linux-arm64': 'kwctl-linux-arm64',
  'linux-x64': 'kwctl-linux-amd64',
};

function platformKey() {
  return `${process.platform}-${process.arch}`;
}

function resolveBinary() {
  const key = platformKey();
  const pkg = PLATFORM_PACKAGES[key];
  if (!pkg) {
    console.error(`kwctl: unsupported platform ${key} (need darwin/linux on arm64 or x64)`);
    process.exit(1);
  }
  try {
    return require.resolve(`${pkg}/bin/kwctl`);
  } catch {
    console.error(
      `kwctl: missing platform package ${pkg}. Try: npm install kwctl --force`
    );
    process.exit(1);
  }
}

const result = spawnSync(resolveBinary(), process.argv.slice(2), {
  stdio: 'inherit',
});

if (result.error) {
  console.error(`kwctl: ${result.error.message}`);
  process.exit(1);
}

process.exit(result.status === null ? 1 : result.status);
