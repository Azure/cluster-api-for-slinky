#!/usr/bin/env python3

# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Generate cyclic Slurm load for autoscaling tests.

The script submits exclusive placeholder jobs with a continuously varying
instantaneous frequency. A sinusoidal envelope controls jobs per minute while
a short integration loop submits each time accumulated job phase crosses an
integer.

Environment variable overrides:
  Every CLI option can also be supplied by its uppercase environment variable,
  such as LOAD_PROFILE, MIN_RATE, JOB_SCRIPT, or DRY_RUN.

Examples:
  ./slurm_load_generator.py
  ./slurm_load_generator.py --profile azure
  ./slurm_load_generator.py --dry-run --max-minutes 8 --minute-seconds 0 --min-rate 0.5 --max-rate 1.5
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


LOAD_PROFILES: dict[str, dict[str, str]] = {
  "local": {
    "MIN_RATE": "1",
    "MAX_RATE": "5",
    "CYCLE_MINUTES": "16",
  },
  "azure": {
    "MIN_RATE": "0.25",
    "MAX_RATE": "4",
    "CYCLE_MINUTES": "120",
  },
}
LOAD_PROFILES["cloud"] = LOAD_PROFILES["azure"]

HALF_CYCLE = 0.5


def float_arg(raw_value: str) -> float:
  try:
    value = float(raw_value)
  except ValueError as exc:
    raise argparse.ArgumentTypeError(
      f"must be a number, got {raw_value!r}"
    ) from exc
  if not math.isfinite(value):
    raise argparse.ArgumentTypeError("must be finite")
  return value


def non_negative_float_arg(raw_value: str) -> float:
  value = float_arg(raw_value)
  if value < 0:
    raise argparse.ArgumentTypeError("must be >= 0")
  return value


def positive_float_arg(raw_value: str) -> float:
  value = float_arg(raw_value)
  if value <= 0:
    raise argparse.ArgumentTypeError("must be > 0")
  return value


def boolean_arg(raw_value: str) -> bool:
  normalized = raw_value.strip().lower()
  if normalized in {"1", "true", "yes", "y", "on"}:
    return True
  if normalized in {"0", "false", "no", "n", "off"}:
    return False
  raise argparse.ArgumentTypeError(f"must be boolean-like, got {raw_value!r}")


def optional_path_arg(raw_value: str) -> Path | None:
  return Path(raw_value) if raw_value else None


def existing_file_arg(raw_value: str) -> Path:
  path = Path(raw_value)
  if not path.is_file():
    raise argparse.ArgumentTypeError(f"file not found: {path}")
  return path


def format_number(value: float) -> str:
  return format(value, "g")


@dataclass(frozen=True)
class Config:
  profile: str
  min_rate: float
  max_rate: float
  cycle_minutes: float
  tick_seconds: float
  job_script: Path
  dry_run: bool
  log_file: Path | None
  minute_seconds: float
  max_minutes: float

  def validate(self) -> None:
    if self.max_rate < self.min_rate:
      raise ValueError("--max-rate must be >= --min-rate")


def rate_at_phase(
  phase: float,
  *,
  min_rate: float,
  max_rate: float,
) -> float:
  wave = (1.0 - math.cos(math.tau * phase)) / 2.0
  return min_rate + (max_rate - min_rate) * wave


def configure_logging(log_file: Path | None) -> logging.Logger:
  handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
  if log_file is not None:
    handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
  logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
    handlers=handlers,
    force=True,
  )
  return logging.getLogger(__name__)


class LoadGenerator:
  def __init__(self, config: Config, logger: logging.Logger) -> None:
    self.config = config
    self.logger = logger
    self.total_jobs = 0
    self.logical_seconds = 0.0
    self.job_phase = 0.0
    self.start_time = time.monotonic()

  @property
  def envelope_phase(self) -> float:
    cycle_seconds = self.config.cycle_minutes * 60.0
    return (self.logical_seconds / cycle_seconds) % 1.0

  @property
  def current_rate(self) -> float:
    return rate_at_phase(
      self.envelope_phase,
      min_rate=self.config.min_rate,
      max_rate=self.config.max_rate,
    )

  @property
  def direction(self) -> str:
    if self.envelope_phase < HALF_CYCLE:
      return "up"
    return "down"

  @property
  def phase_radians(self) -> float:
    return math.tau * self.envelope_phase

  @property
  def cycle_count(self) -> int:
    return int(self.logical_seconds // (self.config.cycle_minutes * 60.0))

  def rate_at(self, logical_seconds: float) -> float:
    phase = (logical_seconds / (self.config.cycle_minutes * 60.0)) % 1.0
    return rate_at_phase(
      phase,
      min_rate=self.config.min_rate,
      max_rate=self.config.max_rate,
    )

  def log_summary(self) -> None:
    elapsed = int(time.monotonic() - self.start_time)
    self.logger.info(
      "SUMMARY: "
      f"runtime={elapsed}s "
      f"total_jobs={self.total_jobs} "
      f"current_rate={format_number(self.current_rate)} "
      f"cycles={self.cycle_count} "
      f"direction={self.direction} "
      f"phase={self.phase_radians:.3f}rad "
      f"job_phase={format_number(self.job_phase)}"
    )

  def submit_job(self, job_index: int, jobs_this_tick: int) -> None:
    if self.config.dry_run:
      self.logger.info(
        f"DRY-RUN sbatch {self.config.job_script} "
        f"at={self.logical_seconds / 60.0:.3f}m "
        f"(job {job_index}/{jobs_this_tick})"
      )
      return

    result = subprocess.run(
      ["sbatch", str(self.config.job_script)],
      check=False,
      capture_output=True,
      text=True,
    )
    if result.returncode != 0:
      stderr = result.stderr.strip() or "no stderr"
      self.logger.error(f"sbatch failed exit={result.returncode}: {stderr}")
      return

    output = result.stdout.strip()
    job_id = output.split()[-1] if output else "?"
    self.logger.info(
      f"Submitted job {job_id} "
      f"at={self.logical_seconds / 60.0:.3f}m "
      f"({job_index}/{jobs_this_tick})"
    )

  def log_state(self) -> None:
    self.logger.info(
      "STATE "
      f"elapsed={self.logical_seconds / 60.0:.1f}m "
      f"rate={format_number(self.current_rate)} "
      f"direction={self.direction} "
      f"phase={self.phase_radians:.3f}rad "
      f"cycles={self.cycle_count} "
      f"job_phase={format_number(self.job_phase)}"
    )

  def run(self) -> None:
    self.logger.info(
      "Starting Slurm load generator: "
      f"LOAD_PROFILE={self.config.profile} "
      f"MIN_RATE={format_number(self.config.min_rate)} "
      f"MAX_RATE={format_number(self.config.max_rate)} "
      f"CYCLE={format_number(self.config.cycle_minutes)}m "
      f"TICK={format_number(self.config.tick_seconds)}s "
      f"JOB_SCRIPT={self.config.job_script}"
    )

    duration_seconds = (
      self.config.max_minutes * 60.0
      if self.config.max_minutes
      else math.inf
    )
    next_state_log = 0.0
    while self.logical_seconds < duration_seconds:
      if self.logical_seconds >= next_state_log:
        self.log_state()
        next_state_log += 60.0

      tick_seconds = min(
        self.config.tick_seconds,
        duration_seconds - self.logical_seconds,
      )
      midpoint = self.logical_seconds + tick_seconds / 2.0
      instantaneous_rate = self.rate_at(midpoint)
      self.job_phase += instantaneous_rate * tick_seconds / 60.0
      jobs_this_tick = math.floor(self.job_phase + 1e-12)
      self.job_phase = max(0.0, self.job_phase - jobs_this_tick)
      if math.isclose(self.job_phase, 0.0, abs_tol=1e-12):
        self.job_phase = 0.0
      self.logical_seconds += tick_seconds

      target_wall_time = self.start_time + (
        self.logical_seconds / 60.0 * self.config.minute_seconds
      )
      sleep_seconds = target_wall_time - time.monotonic()
      if sleep_seconds > 0:
        time.sleep(sleep_seconds)

      for job_index in range(1, jobs_this_tick + 1):
        self.submit_job(job_index, jobs_this_tick)
        self.total_jobs += 1

    self.log_summary()


def selected_profile(argv: list[str] | None) -> str:
  parser = argparse.ArgumentParser(add_help=False)
  parser.add_argument(
    "--profile",
    choices=sorted(LOAD_PROFILES),
  )
  args, _ = parser.parse_known_args(argv)
  if args.profile is not None:
    return args.profile

  env_profile = os.environ.get("LOAD_PROFILE", "local").strip().lower()
  return parser.parse_args(["--profile", env_profile]).profile


def build_parser(profile: str) -> argparse.ArgumentParser:
  defaults = LOAD_PROFILES[profile]
  parser = argparse.ArgumentParser(
    description=(
      "Generate cyclic Slurm placeholder load. CLI flags override environment "
      "variables; environment variables override profile defaults."
    ),
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
  )
  parser.add_argument(
    "--profile",
    choices=sorted(LOAD_PROFILES),
    default=profile,
    help="load profile preset (env: LOAD_PROFILE)",
  )
  parser.add_argument(
    "--min-rate",
    type=non_negative_float_arg,
    default=os.environ.get("MIN_RATE", defaults["MIN_RATE"]),
    help="lowest jobs per minute; fractional allowed (env: MIN_RATE)",
  )
  parser.add_argument(
    "--max-rate",
    type=non_negative_float_arg,
    default=os.environ.get("MAX_RATE", defaults["MAX_RATE"]),
    help="highest jobs per minute; fractional allowed (env: MAX_RATE)",
  )
  parser.add_argument(
    "--cycle-minutes",
    type=positive_float_arg,
    default=os.environ.get("CYCLE_MINUTES", defaults["CYCLE_MINUTES"]),
    help="duration of one min-to-max-to-min cycle (env: CYCLE_MINUTES)",
  )
  parser.add_argument(
    "--tick-seconds",
    type=positive_float_arg,
    default=os.environ.get("TICK_SECONDS", "1"),
    help="logical seconds between frequency integrations (env: TICK_SECONDS)",
  )
  parser.add_argument(
    "--job-script",
    type=existing_file_arg,
    default=os.environ.get("JOB_SCRIPT", "sleep-exclusive.slurm"),
    help="Slurm batch script passed to sbatch (env: JOB_SCRIPT)",
  )
  parser.add_argument(
    "--log-file",
    type=optional_path_arg,
    default=os.environ.get("LOG_FILE", "slurm_load_generator.log"),
    help="append logs to this file; empty disables file logging (env: LOG_FILE)",
  )
  parser.add_argument(
    "--minute-seconds",
    type=non_negative_float_arg,
    default=os.environ.get("MINUTE_SECONDS", "60"),
    help="wall-clock seconds in one logical minute (env: MINUTE_SECONDS)",
  )
  parser.add_argument(
    "--max-minutes",
    type=non_negative_float_arg,
    default=os.environ.get("MAX_MINUTES", "0"),
    help="stop after this many logical minutes; 0 runs forever (env: MAX_MINUTES)",
  )
  parser.add_argument(
    "--dry-run",
    action=argparse.BooleanOptionalAction,
    type=boolean_arg,
    default=os.environ.get("DRY_RUN", "0"),
    help="print submissions without running sbatch (env: DRY_RUN)",
  )
  return parser


def main(argv: list[str] | None = None) -> int:
  parser = build_parser(selected_profile(argv))
  config = Config(**vars(parser.parse_args(argv)))
  try:
    config.validate()
  except ValueError as exc:
    parser.error(str(exc))

  logger = configure_logging(config.log_file)
  generator = LoadGenerator(config, logger)

  def handle_signal(signum: int, _frame: object) -> None:
    logger.info(f"Caught signal {signum}; exiting...")
    generator.log_summary()
    raise SystemExit(128 + signum)

  for handled_signal in (signal.SIGINT, signal.SIGTERM):
    signal.signal(handled_signal, handle_signal)
  generator.run()
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
