# Copyright (c) 2022-2026, The Isaac Lab Project Developers (https://github.com/isaac-sim/IsaacLab/blob/main/CONTRIBUTORS.md).
# All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause

import os
import subprocess
from pathlib import Path

import pytest


def start_stop_docker(profile, suffix):
    """Test starting and stopping docker profile with suffix."""
    environ = os.environ
    context_dir = Path(__file__).resolve().parent.parent

    # generate parameters for the arguments
    if suffix != "":
        container_name = f"isaac-lab-{profile}-{suffix}"
        suffix_args = ["--suffix", suffix]
    else:
        container_name = f"isaac-lab-{profile}"
        suffix_args = []

    run_kwargs = {
        "check": False,
        "capture_output": True,
        "text": True,
        "cwd": context_dir,
        "env": environ,
    }

    # start the container
    docker_start = subprocess.run(["python", "container.py", "start", profile] + suffix_args, **run_kwargs)
    assert docker_start.returncode == 0

    # verify that the container is running
    docker_running_true = subprocess.run(["docker", "ps"], **run_kwargs)
    assert docker_running_true.returncode == 0
    assert container_name in docker_running_true.stdout

    # stop the container
    docker_stop = subprocess.run(["python", "container.py", "stop", profile] + suffix_args, **run_kwargs)
    assert docker_stop.returncode == 0

    # verify that the container has stopped
    docker_running_false = subprocess.run(["docker", "ps"], **run_kwargs)
    assert docker_running_false.returncode == 0
    assert container_name not in docker_running_false.stdout


@pytest.mark.parametrize(
    "profile,suffix",
    [
        ("base", ""),
        ("base", "test"),
        ("ros2", ""),
        ("ros2", "test"),
    ],
)
def test_docker_profiles(profile, suffix):
    """Test starting and stopping docker profiles with and without suffixes."""
    start_stop_docker(profile, suffix)


def build_then_start_no_build_then_stop(profile, suffix):
    """Test that start-no-build works after the image is pre-built."""
    environ = os.environ
    context_dir = Path(__file__).resolve().parent.parent

    if suffix != "":
        container_name = f"isaac-lab-{profile}-{suffix}"
        suffix_args = ["--suffix", suffix]
    else:
        container_name = f"isaac-lab-{profile}"
        suffix_args = []

    run_kwargs = {
        "check": False,
        "capture_output": True,
        "text": True,
        "cwd": context_dir,
        "env": environ,
    }

    # pre-build the image (idempotent if it already exists)
    docker_build = subprocess.run(["python", "container.py", "build", profile] + suffix_args, **run_kwargs)
    assert docker_build.returncode == 0

    # start the container without (re)building
    docker_start = subprocess.run(
        ["python", "container.py", "start-no-build", profile] + suffix_args, **run_kwargs
    )
    assert docker_start.returncode == 0

    # verify that the container is running
    docker_running_true = subprocess.run(["docker", "ps"], **run_kwargs)
    assert docker_running_true.returncode == 0
    assert container_name in docker_running_true.stdout

    # stop the container
    docker_stop = subprocess.run(["python", "container.py", "stop", profile] + suffix_args, **run_kwargs)
    assert docker_stop.returncode == 0

    # verify that the container has stopped
    docker_running_false = subprocess.run(["docker", "ps"], **run_kwargs)
    assert docker_running_false.returncode == 0
    assert container_name not in docker_running_false.stdout


@pytest.mark.parametrize(
    "profile,suffix",
    [
        ("base", ""),
        ("base", "test"),
        ("ros2", ""),
        ("ros2", "test"),
    ],
)
def test_docker_start_no_build(profile, suffix):
    """Test start-no-build path for docker profiles with and without suffixes."""
    build_then_start_no_build_then_stop(profile, suffix)


def test_docker_start_no_build_missing_image():
    """Test that start-no-build fails fast when the suffixed image is absent."""
    environ = os.environ
    context_dir = Path(__file__).resolve().parent.parent

    # Use a suffix unlikely to collide with anything else on the host
    suffix = "no-build-missing-xyz"
    image_name = f"isaac-lab-base-{suffix}:latest"

    # Ensure the image is absent
    subprocess.run(["docker", "image", "rm", image_name], capture_output=True)

    # Invoke start-no-build and expect a non-zero exit with an informative message
    result = subprocess.run(
        ["python", "container.py", "start-no-build", "base", "--suffix", suffix],
        check=False,
        capture_output=True,
        text=True,
        cwd=context_dir,
        env=environ,
    )
    assert result.returncode != 0
    combined = (result.stdout + result.stderr).lower()
    assert "not found" in combined
