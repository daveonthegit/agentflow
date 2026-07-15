#!/usr/bin/env node

import { existsSync, lstatSync, mkdirSync, symlinkSync, unlinkSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { spawnSync } from "node:child_process";

const REPOSITORY_URL =
  process.env.AGENTFLOW_REPOSITORY_URL ??
  "https://github.com/daveonthegit/agentflow.git";

function fail(message) {
  process.stderr.write(`agentflow installer: ${message}\n`);
  process.exit(1);
}

function run(command, args) {
  process.stdout.write(`> ${command} ${args.join(" ")}\n`);
  const result = spawnSync(command, args, { stdio: "inherit" });
  if (result.error) {
    fail(result.error.message);
  }
  if (result.status !== 0) {
    fail(`${command} exited with status ${result.status}`);
  }
}

function available(command) {
  const result = spawnSync(command, ["--version"], { stdio: "ignore" });
  return !result.error && result.status === 0;
}

function pythonExecutable() {
  if (process.env.AGENTFLOW_PYTHON) {
    return process.env.AGENTFLOW_PYTHON;
  }
  for (const candidate of ["python3", "python"]) {
    if (available(candidate)) {
      return candidate;
    }
  }
  fail("Python 3.9 or newer is required but was not found on PATH");
}

const [command, ...flags] = process.argv.slice(2);
if (command !== "install") {
  fail("usage: agentflow-install install [--dry-run]");
}

const supportedFlags = new Set(["--dry-run"]);
for (const flag of flags) {
  if (!supportedFlags.has(flag)) {
    fail(`unknown option: ${flag}`);
  }
}

const dryRun = flags.includes("--dry-run");
const installRoot = resolve(
  process.env.AGENTFLOW_INSTALL_ROOT ?? join(homedir(), ".local", "share", "agentflow"),
);
const source = join(installRoot, "source");
const venv = join(installRoot, "venv");
const binDir = resolve(
  process.env.AGENTFLOW_BIN_DIR ?? join(homedir(), ".local", "bin"),
);
const commandPath = join(binDir, process.platform === "win32" ? "agentflow.exe" : "agentflow");
const venvPython = join(
  venv,
  process.platform === "win32" ? "Scripts" : "bin",
  process.platform === "win32" ? "python.exe" : "python",
);
const venvCommand = join(
  venv,
  process.platform === "win32" ? "Scripts" : "bin",
  process.platform === "win32" ? "agentflow.exe" : "agentflow",
);

const plan = {
  state: "planned",
  repository: REPOSITORY_URL,
  source,
  environment: venv,
  command: commandPath,
  steps: [
    existsSync(source) ? "update_repository" : "clone_repository",
    "create_python_environment",
    "install_cli",
    "expose_command",
    "install_global_skill",
  ],
};

if (dryRun) {
  process.stdout.write(`${JSON.stringify(plan, null, 2)}\n`);
  process.exit(0);
}

if (process.platform === "win32") {
  fail("the first installer release supports macOS and Linux; Windows support is not yet implemented");
}
if (!available("git")) {
  fail("Git is required but was not found on PATH");
}
if (!available("npx")) {
  fail("Node.js with npx is required but was not found on PATH");
}

mkdirSync(installRoot, { recursive: true });
if (!existsSync(source)) {
  run("git", ["clone", "--depth", "1", REPOSITORY_URL, source]);
} else if (existsSync(join(source, ".git"))) {
  run("git", ["-C", source, "pull", "--ff-only"]);
} else {
  fail(`${source} exists but is not a Git repository`);
}

if (!existsSync(venvPython)) {
  run(pythonExecutable(), ["-m", "venv", venv]);
}
run(venvPython, ["-m", "pip", "install", "--upgrade", "pip"]);
run(venvPython, ["-m", "pip", "install", "--editable", source]);

mkdirSync(dirname(commandPath), { recursive: true });
if (existsSync(commandPath)) {
  if (!lstatSync(commandPath).isSymbolicLink()) {
    fail(`${commandPath} already exists and is not an Agentflow-managed symlink`);
  }
  unlinkSync(commandPath);
}
symlinkSync(venvCommand, commandPath);

run("npx", [
  "--yes",
  "skills@latest",
  "add",
  source,
  "--skill",
  "agentflow",
  "-g",
  "-y",
]);

const pathEntries = (process.env.PATH ?? "").split(":");
const warning = pathEntries.includes(binDir)
  ? null
  : `Add ${binDir} to PATH before running agentflow.`;
process.stdout.write(
  `${JSON.stringify({ ...plan, state: "installed", warning }, null, 2)}\n`,
);
