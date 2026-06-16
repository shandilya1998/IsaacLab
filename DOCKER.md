# IsaacLab Docker Setup

## 1. Introduction

This document captures the background, preliminary knowledge, software fundamentals, and concrete implementation details that together define how the IsaacLab Python package is containerised. Its purpose is to function as a single, self-contained reference for daily usage, maintenance, debugging, and extension of the containerised development environment shipped with IsaacLab. The intended reader is an engineer who already operates within the IsaacLab workspace and now needs a clear mental model of the underlying Docker machinery before modifying it.

The scope of the document is intentionally restricted to the `base` profile of the IsaacLab Docker setup, which is the only profile currently in active use in this workspace. The optional `ros2` profile that ships with the upstream project is mentioned where structurally relevant, but its dedicated build artifacts, middleware configuration, and DDS profiles are not documented in depth. Cluster, Singularity, and CloudXR auxiliary flows are also surfaced only at the level required to understand the boundaries of the base setup.

The document is structured in three principal sections. Section 1 (this section) frames the goals and structure of the document and orients the reader toward what follows. Section 2, *Preliminary Knowledge*, supplies a focused refresher on Docker, Docker Compose, and the Docker API and CLI, with examples drawn from the IsaacLab configuration so that each concept is anchored in code the reader can later inspect. Section 3, *IsaacLab Docker Setup*, then walks through the actual implementation: the layout of `docker/`, the construction of `Dockerfile.base`, the merging behaviour of `docker-compose.yaml` with auxiliary YAML files and `.env` files, and the operational interface exposed by `docker/container.py`.

The flow is designed to be cumulative. Each later subsection assumes the vocabulary and primitives introduced in earlier ones, so that by the time the reader reaches the operational interface they are reading it as a thin orchestration layer over already-familiar Docker and Compose primitives. A *References* section at the end collects all internal file paths and external documentation links cited in the body of the document.

## 2. Preliminary Knowledge

This section consolidates the conceptual background a maintainer needs in order to read the IsaacLab Docker setup with confidence. It is organised around three layers that correspond to the layers of the actual implementation: the low-level Docker engine primitives, the higher-level Docker Compose orchestration model, and the command-line and API surface that user-facing scripts ultimately invoke. Each subsection introduces the relevant theory and then immediately points at the location in `docker/` where that idea is exercised.

### 2.1 Docker

Docker is a client–server platform that packages an application and its dependencies into an *image* and then runs that image as an isolated *container* on a host kernel. The platform consists of a long-running daemon, `dockerd`, that performs the heavy lifting of building, running, and distributing containers, and a thin client, `docker`, that talks to the daemon over a REST API [^docker-overview]. The client and daemon may run on the same machine, as they do in the IsaacLab developer setup, or communicate across the network. The container model relies on Linux kernel namespaces and cgroups for isolation, which is why containers are far lighter than full virtual machines while still providing a reproducible runtime environment.

An *image* is a read-only template built from a `Dockerfile`, a plain-text recipe of ordered instructions such as `FROM`, `RUN`, `COPY`, `ENV`, and `WORKDIR`. Each instruction produces a *layer*; layers are content-addressed and cached, so an unchanged instruction reuses its prior layer on subsequent builds. This layering model is what allows `docker/Dockerfile.base` in IsaacLab to start from the large `nvcr.io/nvidia/isaac-sim:5.1.0` image and add only the IsaacLab-specific apt packages, pip dependencies, and convenience shell aliases on top. A *container* is then a runnable instance of an image with a thin writable layer added on top of the immutable image layers.

Containers do not, by default, preserve any state between runs: when a container is removed, its writable layer is destroyed. Persistence is obtained through two complementary mechanisms. *Named volumes* are storage objects managed entirely by Docker and stored under `/var/lib/docker/volumes/<name>/_data` on the host; they are portable, easy to back up, and isolated from the host filesystem layout [^docker-volumes]. *Bind mounts*, by contrast, expose a specific host directory inside the container, which makes them ideal for live source-code editing where changes on the host must be immediately visible in the container.

GPU access requires the NVIDIA Container Toolkit, which extends the runtime so that a container can request `devices` with the `nvidia` driver. The IsaacLab setup relies on this consistently, since both Isaac Sim and PyTorch inside the container expect a CUDA-capable device exposed by the host. Images are distributed through *registries*; IsaacLab pulls its base image from NVIDIA's NGC registry at `nvcr.io`, while custom layers built locally remain on the host's Docker image store unless explicitly pushed elsewhere.

A minimal but representative example of these primitives in action is the first stage of `docker/Dockerfile.base`, where the IsaacLab image is derived from the Isaac Sim image and labelled for traceability:

```dockerfile
ARG ISAACSIM_BASE_IMAGE_ARG=nvcr.io/nvidia/isaac-sim
ARG ISAACSIM_VERSION_ARG=5.1.0
FROM ${ISAACSIM_BASE_IMAGE_ARG}:${ISAACSIM_VERSION_ARG} AS base
ENV ISAACSIM_VERSION=${ISAACSIM_VERSION_ARG}
SHELL ["/bin/bash", "-c"]
LABEL version="2.1.1"
LABEL description="Dockerfile for building and running the Isaac Lab framework inside Isaac Sim container image."
```

This fragment exhibits four primitives at once: the `ARG` directive declares build-time variables that can be overridden by `docker build --build-arg`, `FROM` selects the parent image and names the stage `base` for later multi-stage references, `ENV` promotes a build-time argument into a persistent runtime environment variable, and `LABEL` attaches structured metadata that downstream tooling can query through `docker image inspect`. The same primitives recur throughout the rest of `Dockerfile.base`.

### 2.2 Docker Compose

Docker Compose is a tool for declaring multi-container applications in a single YAML file and operating them as a unit through the `docker compose` command [^compose-features]. Where the raw Docker CLI works one container at a time, Compose lets the maintainer describe *services*, *networks*, *volumes*, and *configs* together with their relationships, and then create, start, stop, and remove them with single commands such as `docker compose up`, `docker compose down`, and `docker compose build`. The IsaacLab setup uses Compose precisely for this reason: it declares the IsaacLab service together with all of its mounts, environment, and GPU reservations in `docker/docker-compose.yaml`.

A *service* in Compose corresponds roughly to a long-lived container specification, including the image to build or pull, the command to run, environment variables, mounted volumes, network mode, and resource reservations. The IsaacLab compose file defines two services, `isaac-lab-base` and `isaac-lab-ros2`, each carrying its own `build:` block, `image:` name, `container_name:`, `environment:`, `volumes:`, `network_mode: host`, and a `deploy:` block that reserves all available NVIDIA GPUs. Services are usually started together, but Compose offers two mechanisms — *profiles* and *file merging* — that let the maintainer activate only a subset or extend a base definition.

*Profiles* solve the problem of optional services that should not run unless explicitly requested. A service decorated with `profiles: [ "ros2" ]` is excluded from `docker compose up` unless the caller passes `--profile ros2` or sets `COMPOSE_PROFILES=ros2`; services without a `profiles` attribute are always active [^compose-profiles]. IsaacLab uses this directly: `isaac-lab-base` belongs to profile `base`, `isaac-lab-ros2` to profile `ros2`, and the orchestration script selects exactly one of them per invocation. This is what makes `./docker/container.py start base` and `./docker/container.py start ros2` two cleanly separated workflows that share the same compose file.

*Extension fields* and *YAML anchors* let the compose file factor out repeated configuration into reusable blocks. A top-level key starting with `x-` is silently ignored by Compose itself but can carry any payload, and a YAML anchor `&name` defines a node that can later be referenced by `*name` or merged with `<<: *name` [^compose-extension]. The IsaacLab compose file uses three such anchors — `x-default-isaac-lab-volumes`, `x-default-isaac-lab-environment`, and `x-default-isaac-lab-deploy` — so that the volumes, environment, and GPU reservation are defined once and aliased into both services. This eliminates drift between `isaac-lab-base` and `isaac-lab-ros2`.

Compose also supplies a powerful *file merging* mechanism through the repeatable `--file` flag. When multiple compose files are passed, mappings are merged with later files overriding earlier ones, sequences are appended, and certain unique-keyed fields such as `volumes` and `ports` are merged by their identifying key [^compose-merge]. The IsaacLab setup exploits this with `docker/x11.yaml`, which redeclares the same `isaac-lab-base` service to add display-related environment variables and bind mounts only when X11 forwarding is enabled, and with `docker-compose.cloudxr-runtime.patch.yaml` for CloudXR streaming. The merging contract means these overlay files never need to repeat the volumes or build context already declared in the base compose file.

Environment variables enter the compose model through two distinct channels. Variable *interpolation* uses the `${VAR}` syntax inside the compose file and is resolved from the shell environment and any `--env-file` passed at the CLI; the IsaacLab compose file uses this to set `image: isaac-lab-base${DOCKER_NAME_SUFFIX-}` so that the final image name is derived at orchestration time. The per-service `env_file:` attribute, by contrast, loads variables that are exported *into the running container's environment*, not into the compose substitution layer. Both `env_file: .env.base` on the service and `--env-file .env.base` on the CLI appear in IsaacLab, and they serve these two different purposes [^compose-env].

### 2.3 Docker API and CLI

The Docker client exposes its functionality through a hierarchical CLI organised around object types: `docker container`, `docker image`, `docker volume`, `docker network`, `docker compose`, and so on. Each group contains the standard verbs the maintainer is likely to invoke — `ls`, `inspect`, `rm`, `prune` — together with type-specific commands such as `docker container exec` and `docker image build`. The legacy top-level forms such as `docker ps`, `docker run`, and `docker build` remain supported and are still in widespread use, including inside `container_interface.py`. Both forms ultimately translate to HTTP calls against the daemon's REST API.

For day-to-day work on the IsaacLab container, the commands that the maintainer must understand are `docker build`, `docker compose build`, `docker compose up --detach`, `docker compose down`, `docker exec`, `docker ps`, `docker container inspect`, `docker image inspect`, and `docker cp`. These nine commands cover the entire surface that `container.py` exposes to the user. The script `docker/utils/container_interface.py` calls them directly via `subprocess.run`, so reading the script effectively documents which CLI invocations correspond to which user-facing action.

A worked example is the running-state check used internally by the orchestration layer:

```python
status = subprocess.run(
    ["docker", "container", "inspect", "-f", "{{.State.Status}}", self.container_name],
    capture_output=True,
    text=True,
    check=False,
).stdout.strip()
return status == "running"
```

This call demonstrates two things at once. First, `docker container inspect` returns rich JSON metadata about a container, and the `-f` flag accepts a Go template that projects out a single field — here `.State.Status`. Second, the same query can be issued ad hoc from the shell to debug whether a container is up, which port mappings it has, or which volumes it mounts, simply by replacing the format string. The same template mechanism applies to `docker image inspect`, and is the recommended way to script around the API without parsing free-form output.

The `docker compose` subcommand groups all multi-service operations and is the primary interface that `container.py` uses. Its key idioms are `compose --file <yaml> --profile <p> --env-file <env> build <service>` to build a specific service under a specific profile, `compose up --detach --build --remove-orphans` to bring up a stack while opportunistically rebuilding, and `compose down --volumes` to tear down both containers and named volumes. The IsaacLab orchestration script assembles exactly these argument lists from its internal state and forwards them to `docker compose`, which means that any maintenance action the script performs can also be performed by hand from the `docker/` directory with the same arguments.

## 3. IsaacLab Docker Setup

This section documents the actual implementation that the preceding background was designed to support. It begins with the on-disk file layout, then ascends through the image definitions, the compose-file orchestration, the auxiliary YAML and environment files, and finally the Python interface exposed through `docker/container.py`. The intent is to leave the reader equipped to perform the everyday operations — building, starting, entering, stopping, copying artifacts — and the slightly less frequent ones — adding a mount, adding a dependency, producing a uniquely named container instance.

### 3.1 File structure

All IsaacLab Docker assets live under the `docker/` directory at the root of the repository. The directory contains the entry-point script `container.py` and its deprecated bash wrapper `container.sh`; the two Dockerfiles `Dockerfile.base` and `Dockerfile.ros2`; an experimental `Dockerfile.curobo` that adds cuRobo on top of the base image; the orchestration file `docker-compose.yaml` together with the overlay files `x11.yaml` and `docker-compose.cloudxr-runtime.patch.yaml`; and the environment files `.env.base`, `.env.ros2`, and `.env.cloudxr-runtime`. A hidden state file, `.container.cfg`, persists user choices such as whether X11 forwarding is enabled, and `.isaac-lab-docker-history` accumulates the shell history of all bash sessions opened inside the container.

Two further subdirectories complete the layout. The `docker/utils/` package collects the Python helpers that implement the orchestration logic: `container_interface.py` for the main `ContainerInterface` class, `state_file.py` for the `StateFile` wrapper around `configparser`, and `x11_utils.py` for the X11 forwarding lifecycle. The `docker/cluster/` directory hosts the remote-cluster workflow built on Singularity/Apptainer, with its own environment file `.env.cluster` and the shell scripts `cluster_interface.sh`, `submit_job_slurm.sh`, `submit_job_pbs.sh`, and `run_singularity.sh`. Cluster usage is out of scope for this document and is mentioned only because its existence explains some of the singularity-friendly idioms baked into `Dockerfile.base`.

A `.dockerignore` file at the repository root governs which files are excluded from the build context. It removes `.git/`, the local `docs/` build, all `logs/`, `runs/`, `outputs/`, `wandb/`, and `videos/` directories, the IsaacLab cluster export folder, the `.container.cfg` state file, the `_isaac_sim` symlink that points to a host installation, the `.isaac-lab-docker-history` file, and the local `env_isaaclab` uv environment. The net effect is that `COPY ../ ${ISAACLAB_PATH}` inside `Dockerfile.base` only ingests source code, scripts, tools, and configuration — never the developer's local logs or build cache — which keeps the image size predictable and avoids accidental leaks of local state into the image.

### 3.2 Docker images

IsaacLab does not ship a single monolithic image; it instead defines a small family of related images that share the same NVIDIA Isaac Sim base. The canonical image is built from `docker/Dockerfile.base` and is tagged `isaac-lab-base:latest` by default. The ROS2 variant in `docker/Dockerfile.ros2` is built *on top of* the base image, so it inherits everything in it and adds the ROS2 Humble packages and DDS configuration; this layering is also visible in its first instruction, `FROM isaac-lab-base${DOCKER_NAME_SUFFIX} AS ros2`. The `Dockerfile.curobo` variant follows a similar approach with the cuRobo motion planning library added.

The choice to derive from `nvcr.io/nvidia/isaac-sim:5.1.0`, configured through `ISAACSIM_BASE_IMAGE` and `ISAACSIM_VERSION` in `.env.base`, is what gives the resulting image a working Isaac Sim runtime, Omniverse Kit, and CUDA-capable Python environment without IsaacLab having to assemble any of those pieces itself. The `Dockerfile.base` then adds three groups of changes: build-time arguments and labels for traceability, apt and pip dependencies required by IsaacLab extensions, and a small set of conveniences — a `_isaac_sim` symlink, bashrc aliases, and pre-created cache directories — that make the running container usable interactively.

### 3.3 The base Dockerfile in depth

`Dockerfile.base` begins by re-declaring the build arguments it expects from the compose layer. Three are required for the image to be functional: `ISAACSIM_ROOT_PATH_ARG`, `ISAACLAB_PATH_ARG`, and `DOCKER_USER_HOME_ARG`. Each is captured into a corresponding `ENV` so that later `RUN` instructions and the runtime shell both see the same value. The `LANG=C.UTF-8` and `DEBIAN_FRONTEND=noninteractive` settings are conventional choices that prevent locale and prompt issues during apt installation. The shell is forced to bash via `SHELL ["/bin/bash", "-c"]` so that `&&`-chained commands behave consistently.

System dependencies are installed in a single `RUN` block that mounts the apt cache so that repeated builds can reuse previously downloaded archives:

```dockerfile
RUN --mount=type=cache,target=/var/cache/apt \
    apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    libglib2.0-0 \
    ncurses-term \
    wget && \
    apt -y autoremove && apt clean autoclean && \
    rm -rf /var/lib/apt/lists/*
```

The `--mount=type=cache` syntax is a BuildKit feature that creates a build-time cache mount which persists across builds without becoming a layer of the image itself. Combining it with `apt clean autoclean` and `rm -rf /var/lib/apt/lists/*` in the same `RUN` is a deliberate idiom: BuildKit benefits from the cached package archives during repeat builds, while the final layer remains small because the package lists are deleted before the layer is committed.

The IsaacLab source tree is then copied into place with `COPY ../ ${ISAACLAB_PATH}`. The `../` path is resolved relative to the build context declared in `docker-compose.yaml`, which is the repository root, so this single instruction ingests every tracked file the `.dockerignore` did not exclude. Immediately afterwards, `isaaclab.sh` is made executable and the symbolic link `${ISAACLAB_PATH}/_isaac_sim -> ${ISAACSIM_ROOT_PATH}` is created so that the in-repo tooling can reach the Isaac Sim Python interpreter through the same path it would use on a binary host install.

The Python dependency installation step uses pip caching for the same reason apt caching was used earlier — the `isaaclab.sh --install` command pulls a non-trivial set of large wheels and benefits substantially from a reusable cache:

```dockerfile
RUN --mount=type=cache,target=${DOCKER_USER_HOME}/.cache/pip \
    ${ISAACLAB_PATH}/_isaac_sim/python.sh -m pip install --upgrade pip==23 setuptools==65 cma==4.4.4 && \
    ${ISAACLAB_PATH}/isaaclab.sh --install
```

The first invocation pins the build-front-end tools to versions known to be compatible with the Isaac Sim Python interpreter, and the second invocation defers to `isaaclab.sh --install`, the canonical IsaacLab installer, to install every Python extension shipped under `source/`. A subsequent `pip uninstall -y quadprog` step removes a transitively pulled dependency that is documented in-source as problematic, which is the kind of pragmatic detail a maintainer should be aware of when reproducing the environment outside the container.

The final blocks of `Dockerfile.base` perform two further actions. First, a sequence of `mkdir -p` and `touch` commands creates empty directories and binary placeholders under `${ISAACSIM_ROOT_PATH}/kit/cache`, `${DOCKER_USER_HOME}/.cache/*`, `/bin/nvidia-smi`, and `/var/run/nvidia-persistenced/socket`. These are required only by Singularity/Apptainer, which binds host paths into the container and refuses to do so if the in-container target does not already exist; their presence is harmless under plain Docker. Second, a series of `echo ... >> ${HOME}/.bashrc` commands defines the `isaaclab`, `python`, `python3`, `pip`, `pip3`, and `tensorboard` aliases inside the container so that an interactive shell behaves as the IsaacLab documentation expects.

### 3.4 The compose orchestration

`docker/docker-compose.yaml` defines the runtime configuration of the IsaacLab container family. The file begins with three YAML anchors that capture the shared portions of the service definitions: `x-default-isaac-lab-volumes` enumerates every volume and bind mount used at runtime, `x-default-isaac-lab-environment` sets `ISAACSIM_PATH` and `OMNI_KIT_ALLOW_ROOT`, and `x-default-isaac-lab-deploy` declares the GPU reservation block. These anchors are then aliased into both services with `volumes: *default-isaac-lab-volumes`, `environment: *default-isaac-lab-environment`, and `deploy: *default-isaac-lab-deploy`, which guarantees that the base and ROS2 variants stay structurally identical.

The volumes block is the most operationally important part of the file because it defines what survives a `docker compose down` and what is shared with the host filesystem. The Isaac Sim caches — `isaac-cache-kit`, `isaac-cache-ov`, `isaac-cache-pip`, `isaac-cache-gl`, `isaac-cache-compute` — are declared as Docker-managed named volumes; they keep shader, pip, and Omniverse caches warm between container lifecycles, which substantially accelerates subsequent starts. The IsaacLab artifact directories — `isaac-lab-docs` for `docs/_build`, and `isaac-lab-data` for `data_storage` — use the same named-volume mechanism for the dual purpose of avoiding root-owned files on the host and preserving artifacts for later extraction via `container.py copy`.

Live editing of source code is achieved through bind mounts of `../source`, `../scripts`, `../docs`, `../tools`, and `../logs` into the in-container `${DOCKER_ISAACLAB_PATH}` (resolved to `/workspace/isaaclab` from `.env.base`). Because bind mounts simply expose a host directory inside the container, any edit made on the host is immediately visible inside the container without rebuilding the image. A final bind mount of `.isaac-lab-docker-history` to `${DOCKER_USER_HOME}/.bash_history` causes the shell history to persist across container restarts, which is convenient during long debugging sessions.

The `isaac-lab-base` service itself reads as a relatively short declaration once the anchors are taken into account:

```yaml
services:
  isaac-lab-base:
    profiles: [ "base" ]
    env_file: .env.base
    build:
      context: ../
      dockerfile: docker/Dockerfile.base
      args:
        - ISAACSIM_BASE_IMAGE_ARG=${ISAACSIM_BASE_IMAGE}
        - ISAACSIM_VERSION_ARG=${ISAACSIM_VERSION}
        - ISAACSIM_ROOT_PATH_ARG=${DOCKER_ISAACSIM_ROOT_PATH}
        - ISAACLAB_PATH_ARG=${DOCKER_ISAACLAB_PATH}
        - DOCKER_USER_HOME_ARG=${DOCKER_USER_HOME}
    image: isaac-lab-base${DOCKER_NAME_SUFFIX-}
    container_name: isaac-lab-base${DOCKER_NAME_SUFFIX-}
    environment: *default-isaac-lab-environment
    volumes: *default-isaac-lab-volumes
    network_mode: host
    deploy: *default-isaac-lab-deploy
    entrypoint: bash
    stdin_open: true
    tty: true
```

This block makes a number of design decisions explicit. The `profiles: [ "base" ]` line keeps the service inactive unless `--profile base` is passed, which is precisely what `container.py` does. The `build.context: ../` line locates the build context at the repository root so that `COPY ../` inside the Dockerfile is well-defined. The `image:` and `container_name:` lines both interpolate `${DOCKER_NAME_SUFFIX-}` so that a single value, supplied at orchestration time, propagates to both the image tag and the container name. The `${VAR-}` form yields an empty string when the variable is unset, which is exactly the default behaviour needed when no suffix is provided.

The `network_mode: host` declaration removes the container from Docker's default bridge network and gives it direct access to the host's network stack. This is required by Isaac Sim for Omniverse Kit's discovery protocols and by ROS2 for DDS multicast, and it is also why no `ports:` mapping is needed on the IsaacLab services. The `deploy:` block requests every NVIDIA GPU on the host with the `gpu` capability; together with the NVIDIA Container Toolkit it is what enables CUDA inside the container. The `entrypoint: bash` together with `stdin_open: true` and `tty: true` keeps the container alive by attaching it to an interactive shell rather than a transient command.

The `isaac-lab-ros2` service is structurally similar but differs in three ways: it carries `profiles: [ "ros2" ]`, it loads both `.env.base` and `.env.ros2` through a list-valued `env_file:`, and it passes `DOCKER_NAME_SUFFIX` as a build argument so that its `FROM isaac-lab-base${DOCKER_NAME_SUFFIX}` first line resolves to the correct base image. This last detail is what allows a suffixed ROS2 image, for example `isaac-lab-ros2-custom`, to be layered on top of a suffixed base image, `isaac-lab-base-custom`, without ambiguity. The same suffix propagation mechanism is what supports running multiple distinct IsaacLab instances side by side, which is discussed further in the daily-use subsection.

### 3.5 The X11 and CloudXR overlay files

`docker/x11.yaml` is a Compose overlay that is conditionally merged into the orchestration when X11 forwarding is enabled. It redeclares the `isaac-lab-base` and `isaac-lab-ros2` services with only the additions needed for GUI forwarding: the `DISPLAY`, `TERM`, `QT_X11_NO_MITSHM`, and `XAUTHORITY` environment variables, a bind mount of the auto-generated `.xauth` temporary directory at `${__ISAACLAB_TMP_DIR}`, a bind mount of the host's `/tmp/.X11-unix` socket directory, and a read-only bind mount of `/etc/localtime`. Because Compose merges sequences by append and unique-keyed fields by target, this overlay adds these entries to the volumes already declared by the base file rather than replacing them.

The `__ISAACLAB_TMP_XAUTH` and `__ISAACLAB_TMP_DIR` variables consumed by `x11.yaml` are not stored in `.env.base`; they are produced at orchestration time by `docker/utils/x11_utils.py` and injected directly into the environment passed to `docker compose`. The same module persists the user's X11 choice in `docker/.container.cfg`, which is a small INI file managed by the `StateFile` wrapper in `docker/utils/state_file.py`. This is a clean separation: persistent user choices live in `.container.cfg`, ephemeral session state lives in shell environment variables, and the compose overlay simply interpolates whichever variables happen to be defined.

`docker-compose.cloudxr-runtime.patch.yaml` follows the same overlay pattern for NVIDIA's CloudXR streaming runtime. It introduces an additional `cloudxr-runtime` service, exposes the signaling and media UDP/TCP ports needed by CloudXR, declares an `openxr-volume` named volume, and amends `isaac-lab-base` with two extra environment variables and a `depends_on: cloudxr-runtime` clause. As with X11, this overlay is opt-in: it is loaded by passing `--file docker-compose.cloudxr-runtime.patch.yaml --env-file .env.cloudxr-runtime` to `container.py start`. The base setup is not affected when CloudXR is not requested.

### 3.6 Environment files

The IsaacLab Docker setup is parameterised through three concentric layers of `.env` files. `docker/.env.base` is the root layer and is always loaded; it declares the Isaac Sim image and version, the in-container paths `DOCKER_ISAACSIM_ROOT_PATH=/isaac-sim`, `DOCKER_ISAACLAB_PATH=/workspace/isaaclab`, `DOCKER_USER_HOME=/root`, the `ACCEPT_EULA=Y` flag, and the initial `DOCKER_NAME_SUFFIX=""`. These variables drive both the build arguments of `Dockerfile.base` and the interpolation of paths inside `docker-compose.yaml`, so changes here propagate throughout the system.

`docker/.env.ros2` is layered on top of `.env.base` whenever the ROS2 profile is active. It specifies the ROS2 apt package variant via `ROS2_APT_PACKAGE=ros-base`, selects the DDS middleware via `RMW_IMPLEMENTATION=rmw_fastrtps_cpp`, and points the FastDDS and CycloneDDS configuration loaders at the XML profiles shipped under `docker/.ros/`. Because Compose appends `--env-file` entries in order, later files override earlier ones on collision, which means a value set in `.env.ros2` will mask the same key set in `.env.base`.

The third layer is opt-in and is supplied by the user through the `--env-files` argument of `container.py`. This is the recommended mechanism for environment variables that are project-specific or developer-specific and should not be committed under `docker/`. The same mechanism is used internally by the CloudXR flow, which loads `.env.cloudxr-runtime` to set the CloudXR image name and version. A note on parsing: `ContainerInterface._parse_dot_vars` reads these `.env` files with a naive `line.strip().split("=", 1)`, so values containing literal `#` characters or shell expansions are not interpreted, only string-substituted into the compose layer.

### 3.7 The `container.py` orchestration script

`docker/container.py` is the user-facing entry point for the entire Docker setup, replacing the older `container.sh` wrapper which is now retained only for backward compatibility and prints a deprecation warning on every invocation. The script defines a single `argparse` parser with seven subcommands — `build`, `start`, `start-no-build`, `enter`, `config`, `copy`, and `stop` — and a small set of common options inherited by all subcommands through an `argparse` parent parser. The common options are `profile`, `--files`, `--env-files`, `--suffix`, and `--info`, and they fully determine which compose files, env files, image name, and container name will be used for the operation.

Internally the script delegates almost all work to `ContainerInterface`, a class defined in `docker/utils/container_interface.py`. The interface is constructed once per invocation and given the context directory, the profile, the additional yamls, the additional env files, and the suffix; it then computes the canonical names that every later command will use:

```python
self.profile = profile
if self.profile == "isaaclab":
    self.profile = "base"
if suffix is None or suffix == "":
    self.suffix = ""
else:
    self.suffix = f"-{suffix}"
self.base_service_name = "isaac-lab-base"
self.service_name = f"isaac-lab-{self.profile}"
self.container_name = f"{self.service_name}{self.suffix}"
self.image_name = f"{self.service_name}{self.suffix}:latest"
self.environ = os.environ.copy()
self.environ["DOCKER_NAME_SUFFIX"] = self.suffix
```

This block captures the entire naming contract of the IsaacLab Docker setup in a few lines. A profile of `base` and a suffix of `custom` yield `service_name=isaac-lab-base`, `container_name=isaac-lab-base-custom`, and `image_name=isaac-lab-base-custom:latest`. The leading hyphen between profile and suffix is inserted automatically only when the suffix is non-empty; an empty suffix leaves the names as plain `isaac-lab-base`. The `DOCKER_NAME_SUFFIX` environment variable, which interpolates into `image: isaac-lab-base${DOCKER_NAME_SUFFIX-}` and `container_name: isaac-lab-base${DOCKER_NAME_SUFFIX-}` in `docker-compose.yaml`, is set to exactly this value before any `docker compose` call is made.

The `_resolve_image_extension` helper then assembles the lists of arguments that will be forwarded to `docker compose`. The base argument list is `["--file", "docker-compose.yaml", "--profile", profile, "--env-file", ".env.base"]`; when the profile is anything other than `base`, the profile-specific env file `.env.{profile}` is appended; when the user passes additional `--files` or `--env-files`, those are appended in order as further `--file` and `--env-file` arguments. The same lists are reused by every subcommand, so the same overlay configuration applies to `build`, `start`, `stop`, and `config` without re-deriving anything.

Each subcommand maps to an idiomatic `docker` or `docker compose` invocation. `build` runs `docker compose ... build <service>`; `start` first ensures the base image is built and then runs `docker compose ... up --detach --build --remove-orphans`; `enter` checks `docker container inspect ... State.Status` and on success runs `docker exec --interactive --tty <container> bash` with `DISPLAY` forwarded from the host environment if present; `stop` runs `docker compose ... down --volumes`; `copy` checks that the container is running and then issues `docker cp` for the `logs`, `docs/_build`, and `data_storage` directories into a local `artifacts/` folder; and `config` runs `docker compose ... config`, optionally with `--output`, to print or save the fully resolved compose configuration. The `--info` flag short-circuits all of this and prints the computed names and argument lists for debugging.

The `start-no-build` subcommand is a companion to `start` intended for the common case of spawning additional containers from an image that is already present on the host. It is exposed through `ContainerInterface.start_no_build`, which uses the existing `does_image_exist` helper to validate that `self.image_name` resolves to an actual image and exits with an explanatory error if it does not. When the image is present, the method issues `docker compose ... up --detach --no-build --remove-orphans` against the same compose-argument lists that `start` would assemble, preserving the X11 overlay, env-file layering, and `.isaac-lab-docker-history` setup. Unlike `start`, it deliberately skips the build-base-first step for non-base profiles, since the caller is expected to have built or tagged the corresponding suffixed image up front.

### 3.8 The supporting Python utilities

`docker/utils/state_file.py` provides `StateFile`, a thin wrapper around `configparser.ConfigParser` that loads `.container.cfg` on construction, persists it on destruction, and offers `set_variable`, `get_variable`, and `delete_variable` methods scoped to a section. The current setup uses a single section, `[X11]`, but the design is deliberately general: any future per-installation preference, for example a remembered registry or a remembered GPU index, could be added by introducing a new namespace.

`docker/utils/x11_utils.py` is the only place outside `container.py` that interacts with both the host environment and the state file. Its `x11_check` function reads `X11_FORWARDING_ENABLED` from `.container.cfg` and, if absent, prompts the user interactively before persisting the answer. When forwarding is enabled, `configure_x11` allocates a temporary `.xauth` file via `mktemp`, populates it with an MIT-MAGIC-COOKIE derived from `xauth nlist $DISPLAY`, and exposes the temporary file and directory as the `__ISAACLAB_TMP_XAUTH` and `__ISAACLAB_TMP_DIR` variables consumed by `x11.yaml`. The companion functions `x11_refresh` and `x11_cleanup` regenerate or remove the temporary file at appropriate points in the container lifecycle.

The package's `__init__.py` exposes only `ContainerInterface`, which is sufficient for the entry-point script. The `x11_utils` module is imported directly from `container.py` via `from utils import ContainerInterface, x11_utils`, which is why the script can reach `x11_utils.x11_check`, `x11_utils.x11_refresh`, and `x11_utils.x11_cleanup` without going through the interface object. This separation keeps the X11 lifecycle out of the otherwise-domain-agnostic `ContainerInterface` class.

### 3.9 Producing uniquely named containers

A core operational concern for any developer running multiple IsaacLab containers on the same host — for example one stable training run and one experimental debug session — is name uniqueness. Both the image tag and the container name are derived from the same `${DOCKER_NAME_SUFFIX}` value, so giving each instance a distinct suffix produces a fully isolated pair of resources. The mechanism is exposed at the CLI as the `--suffix` flag on every subcommand:

```bash
./docker/container.py start base --suffix custom
./docker/container.py start base --suffix experiment-a
./docker/container.py enter base --suffix custom
./docker/container.py stop  base --suffix experiment-a
```

Each invocation in the example above results in a distinct image and container — `isaac-lab-base-custom:latest` and `isaac-lab-base-custom` for the first command, `isaac-lab-base-experiment-a:latest` and `isaac-lab-base-experiment-a` for the second — and the `enter` and `stop` calls disambiguate by the same suffix. Without a suffix, every invocation reuses `isaac-lab-base:latest` and `isaac-lab-base`, which causes the second `start` to silently take over the first instance.

There are three caveats worth knowing. First, named volumes such as `isaac-cache-kit` are *not* suffixed in `docker-compose.yaml`, so two suffixed instances of the same profile will share the same cache volumes; this is desirable for Isaac Sim caches but means concurrent access patterns to `data_storage` or `logs` must be considered if both containers write to them. Second, because `network_mode: host` removes container network isolation, two instances cannot listen on the same port simultaneously, which becomes relevant if both are configured to start a TensorBoard or HTTP server. Third, the ROS2 overlay reads `DOCKER_NAME_SUFFIX` as a build argument, so a suffixed ROS2 build correctly chains onto the suffixed base image; rebuilding the base image with a new suffix is therefore enough to rebuild a paired ROS2 image with the same suffix.

When the goal is to spawn additional containers from a single canonical build rather than to rebuild per suffix, the recommended workflow combines `docker tag` with `start-no-build`. The canonical image is built once under its plain name, retagged under each desired suffix, and started without an additional build pass:

```bash
# Build the canonical image once (or reuse an existing build)
./docker/container.py build base

# Spawn one container per developer without rebuilding
docker tag isaac-lab-base:latest isaac-lab-base-alice:latest
./docker/container.py start-no-build base --suffix alice

docker tag isaac-lab-base:latest isaac-lab-base-bob:latest
./docker/container.py start-no-build base --suffix bob
```

This pattern is the intended boundary between IsaacLab's own tooling and external wrappers such as user-level shell scripts: the wrapper is responsible only for ensuring the suffixed image tag exists, while every other concern — X11 lifecycle, env-file layering, compose argument assembly, history-file setup — remains inside `ContainerInterface`. The `enter`, `copy`, and `stop` subcommands continue to work against the suffixed container exactly as they do for a `start`-created one.

### 3.10 Adding mounts, dependencies, and overlays

Three classes of change recur during the lifetime of the IsaacLab Docker setup. Adding a new directory bind mount is done by appending an entry to the `x-default-isaac-lab-volumes` anchor in `docker-compose.yaml`; both services pick the change up automatically because they alias the anchor with `volumes: *default-isaac-lab-volumes`. The recommended idiom is to mirror the existing entries, providing both a `source:` resolved relative to `docker/` and a `target:` inside the container, which keeps editing semantics consistent across the codebase.

Adding a runtime Python dependency that should live in the image is done by adding it to the relevant IsaacLab extension `setup.py`/`extension.toml` rather than by editing `Dockerfile.base`. The `${ISAACLAB_PATH}/isaaclab.sh --install` step picks up such changes automatically on the next image rebuild. System-level apt dependencies that an extension declares in its `extension.toml` are similarly picked up by `${ISAACLAB_PATH}/tools/install_deps.py apt`, which is invoked from inside the Dockerfile. The Dockerfile itself only needs to be edited when a dependency that does not belong to any extension is required, for example a new CUDA toolkit version.

Adding an overlay compose file is the cleanest way to introduce optional services or environment changes that should not pollute the base setup. The author of the overlay declares only the deltas under each service it wants to extend, and then end users opt in with `--files my-overlay.yaml --env-files my-overlay.env`. This is the pattern that `x11.yaml` and `docker-compose.cloudxr-runtime.patch.yaml` use, and it is the recommended pattern for any future per-team or per-project customisation. Because Compose merges by appending sequences and overriding mappings, an overlay never needs to repeat the volumes, build context, or anchors of the base file.

### 3.11 Daily operational reference

The everyday workflow is short. A first-time setup runs `./docker/container.py start base`, which builds `isaac-lab-base:latest`, creates the named volumes, and starts the container in the background; the script will prompt once for X11 forwarding and record the answer. A subsequent `./docker/container.py enter base` opens an interactive bash inside the running container at `/workspace/isaaclab`, with the `isaaclab`, `python`, `python3`, `pip`, and `tensorboard` aliases ready. After a training run, `./docker/container.py copy base` extracts `logs/`, `docs/_build/`, and `data_storage/` into `docker/artifacts/`, and `./docker/container.py stop base` then tears the container down and removes its named volumes.

Two diagnostic commands are particularly useful when something looks wrong. Passing `--info` to any subcommand short-circuits the operation and prints the computed image and container names, the resolved compose file list, the active profiles, and the resolved env-file list, which makes name and suffix mistakes immediately visible — for example, `./docker/container.py start base --suffix custom --info`. Separately, `./docker/container.py config base` runs `docker compose config` against the same arguments the script would use, producing a fully interpolated compose file on stdout; this is the most reliable way to confirm what `${DOCKER_NAME_SUFFIX}` or any other variable expanded to in the current invocation.

Pre-built IsaacLab images are also published on NVIDIA NGC for tagged releases — at the time of writing the most recent is `nvcr.io/nvidia/isaac-lab:2.3.2` — and can be pulled and run with a plain `docker run` invocation if a from-source build is not required. These images are intended for headless execution and do not include the development-time bind mounts, so they are most appropriate for benchmarking and CI usage rather than day-to-day development. For development, the from-source flow described above remains the supported path.

### 3.12 Note on the ROS2 profile

Although out of scope for daily use in this workspace, the ROS2 profile is worth understanding at a high level because it demonstrates how the orchestration generalises. The profile is activated with `./docker/container.py start ros2`, which causes Compose to merge `.env.base` with `.env.ros2`, build `Dockerfile.ros2` on top of the previously built base image, and create a `isaac-lab-ros2` container that shares all volumes and environment with the base service. ROS2 Humble, both FastDDS and CycloneDDS middlewares, and the DDS configuration files in `docker/.ros/` are added on top, and a `source /opt/ros/humble/setup.bash` line is appended to the container's `.bashrc`. The same suffix and overlay mechanisms documented for the base profile apply unchanged.

### 3.13 Caveat: shared compose project name across suffixed instances

A subtle but consequential behaviour of the orchestration is that issuing `start` or `start-no-build` for one suffix terminates any previously running container belonging to the same profile under a different suffix. Compose tags every container it creates with two labels, `com.docker.compose.project` and `com.docker.compose.service`, and uses these labels on subsequent commands to discover which containers belong to which project. The project name is derived from the directory in which `docker compose` is invoked unless overridden by `--project-name`, the `COMPOSE_PROJECT_NAME` environment variable, or a top-level `name:` key in the YAML; none of these are set by the IsaacLab setup. Every invocation of `container.py` is therefore launched against the same implicit project `docker`.

Because the `--suffix` flag changes only the `container_name` and `image` fields and not the service name itself (which remains `isaac-lab-base` for every base-profile run), all suffixed containers also share the label `com.docker.compose.service=isaac-lab-base`. When a new `docker compose up` is then issued, Compose performs its standard reconciliation: it enumerates containers carrying the project+service labels, compares them to the current desired configuration, and recreates any container whose `container_name` no longer matches. A previously running suffixed container thus appears to Compose as a drifted instance of the same service and is stopped and removed before the new one is created, even though the operator considered the two suffixes independent. The labels on any running container can be confirmed directly with `docker container inspect <name> -f '{{ index .Config.Labels "com.docker.compose.project" }} / {{ index .Config.Labels "com.docker.compose.service" }}'`, which prints `docker / isaac-lab-base` for every suffixed base-profile instance.

The `--remove-orphans` flag passed by `ContainerInterface.start` (line 197) and `ContainerInterface.start_no_build` (line 238) of `docker/utils/container_interface.py` reinforces this behaviour by removing any other containers in the project that are not part of the current desired set, but the core mechanism is the per-service reconciliation rather than orphan removal. Dropping the flag would not change the outcome, because the previously running container still belongs to the same project+service and Compose still considers it the single instance to reconcile against. Similarly, suffixing the `container_name` alone is insufficient as long as the service name and project name remain shared.

The remedy is to scope each suffixed instance to its own Compose project so that prior containers are invisible to subsequent invocations. The least invasive way to do this is to set `COMPOSE_PROJECT_NAME=isaaclab{self.suffix}` in `self.environ` inside `ContainerInterface.__init__`, or equivalently to append `["--project-name", f"isaaclab{self.suffix}"]` to the argument lists assembled in `_resolve_image_extension`. Either change is local to `container_interface.py` and does not require touching the compose files or the `.env` files. Until such a change is applied, operators running multiple suffixed instances should treat `start` and `start-no-build` as mutually exclusive across suffixes for any given profile, and rely on `docker exec` against the surviving suffixed container rather than launching a second one.

### 3.14 GPU exposure and restricting to a single GPU

GPU access for the IsaacLab containers is governed by a single mechanism: the `deploy.resources.reservations.devices` block, which is defined once as the `x-default-isaac-lab-deploy` anchor and aliased into both services with `deploy: *default-isaac-lab-deploy`. There is no `NVIDIA_VISIBLE_DEVICES` or `CUDA_VISIBLE_DEVICES` set anywhere in `docker-compose.yaml` or `.env.base`, so device selection is determined entirely by this anchor. In `docker/docker-compose.yaml` the anchor reads:

```yaml
x-default-isaac-lab-deploy: &default-isaac-lab-deploy
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: all
          capabilities: [ gpu ]
```

Three fields do the work. The `driver: nvidia` and `capabilities: [ gpu ]` lines instruct Docker to satisfy the reservation through the **NVIDIA Container Toolkit**, the host-side runtime extension that injects the GPU devices, the driver libraries, and `nvidia-smi` into the container at start time; without that toolkit installed and registered with the daemon, the reservation cannot be honoured and the container fails to start. The `count: all` line is what makes *every* NVIDIA GPU on the host visible inside the container. The anchor is the only place this is declared, so editing it changes GPU visibility for both `isaac-lab-base` and `isaac-lab-ros2` at once. The relevant reference files are therefore `docker/docker-compose.yaml` (the anchor at the top of the file and the two `deploy: *default-isaac-lab-deploy` aliases on the services) and, on the host side, the NVIDIA Container Toolkit installation; nothing in `Dockerfile.base`, `container.py`, or the `.env` files participates in device selection.

To make exactly one GPU visible inside the container, the `count`/`device_ids` pair in this anchor is the only thing that needs to change — Compose treats `count:` and `device_ids:` as mutually exclusive, so a reservation uses one or the other. There are three practical variants.

To expose any single GPU and let the toolkit choose which one, replace `count: all` with `count: 1`:

```yaml
        - driver: nvidia
          count: 1
          capabilities: [ gpu ]
```

To pin a specific physical GPU by its index, drop `count:` entirely and list the device under `device_ids:`:

```yaml
        - driver: nvidia
          device_ids: [ "0" ]   # expose only GPU 0
          capabilities: [ gpu ]
```

To keep the compose file generic and choose the GPU per invocation, interpolate an environment variable into `device_ids:` and supply it through an opt-in env file passed with `--env-files` (the third env layer described in §3.6, which feeds the compose interpolation layer):

```yaml
        - driver: nvidia
          device_ids: [ "${ISAACLAB_GPU_ID:-0}" ]
          capabilities: [ gpu ]
```

With this last form, `ISAACLAB_GPU_ID=2 ./docker/container.py start base` — or the same key set in an env file handed to `--env-files` — exposes only GPU 2, while an unset variable falls back to GPU 0. After any of these edits, `./docker/container.py config base` prints the fully interpolated `deploy` block so the resolved `device_ids` can be confirmed before launching, and `nvidia-smi` run inside the container (via `./docker/container.py enter base`) should then list exactly one device.

One distinction is worth keeping in mind. Restricting the reservation through `count` or `device_ids` is enforced by the NVIDIA Container Toolkit at the device-injection level, so the other GPUs are genuinely absent from the container and cannot be reached. Setting `CUDA_VISIBLE_DEVICES` inside an already-GPU-enabled container is a weaker, software-level filter applied on top of whatever devices were injected; it hides GPUs from CUDA-aware libraries but does not change what the container can physically see. For true single-GPU isolation, prefer the `deploy`-block approach documented here over `CUDA_VISIBLE_DEVICES`.

## References

### Internal source files (relative to repository root)

- `docker/container.py` — entry-point script for build/start/enter/stop/copy/config commands.
- `docker/container.sh` — deprecated bash wrapper, prints warning and delegates to `container.py`.
- `docker/Dockerfile.base` — base IsaacLab image derived from `nvcr.io/nvidia/isaac-sim:5.1.0`.
- `docker/Dockerfile.ros2` — ROS2 Humble layer on top of the base image.
- `docker/Dockerfile.curobo` — experimental cuRobo variant on top of the base image.
- `docker/docker-compose.yaml` — orchestration definition with services, volumes, anchors, profiles.
- `docker/x11.yaml` — overlay enabling X11 forwarding.
- `docker/docker-compose.cloudxr-runtime.patch.yaml` — overlay enabling CloudXR streaming.
- `docker/.env.base` — root environment file with image, version, and path variables.
- `docker/.env.ros2` — ROS2-specific environment file (`ROS2_APT_PACKAGE`, `RMW_IMPLEMENTATION`).
- `docker/.env.cloudxr-runtime` — CloudXR-specific environment file.
- `docker/.container.cfg` — persisted user preferences (X11 forwarding state, temp file paths).
- `docker/.isaac-lab-docker-history` — host-side bash history persisted across container lifecycles.
- `docker/.ros/fastdds.xml`, `docker/.ros/cyclonedds.xml` — DDS profiles for the ROS2 service.
- `docker/utils/container_interface.py` — implements `ContainerInterface` orchestrating compose calls.
- `docker/utils/state_file.py` — `StateFile` wrapper around `configparser.ConfigParser`.
- `docker/utils/x11_utils.py` — X11 lifecycle helpers (`x11_check`, `configure_x11`, `x11_refresh`, `x11_cleanup`).
- `docker/cluster/` — Singularity-based cluster workflow (out of scope of this document).
- `docker/test/test_docker.py` — pytest harness exercising start/stop for `base` and `ros2` with and without suffix.
- `.dockerignore` — build context exclusions at the repository root.
- `isaaclab.sh` — repository-level installer invoked from inside `Dockerfile.base`.

### External documentation

[^docker-overview]: Docker, "Docker overview." <https://docs.docker.com/get-started/docker-overview/>
[^docker-volumes]: Docker, "Volumes." <https://docs.docker.com/engine/storage/volumes/>
[^compose-features]: Docker, "Docker Compose: Features and uses." <https://docs.docker.com/compose/intro/features-uses/>
[^compose-profiles]: Docker, "Use profiles with Compose." <https://docs.docker.com/compose/how-tos/profiles/>
[^compose-extension]: Docker, "Compose file: extension." <https://docs.docker.com/reference/compose-file/extension/>
[^compose-merge]: Docker, "Merge Compose files." <https://docs.docker.com/reference/compose-file/merge/>
[^compose-env]: Docker, "Environment variables in Compose." <https://docs.docker.com/compose/how-tos/environment-variables/>

### Additional upstream resources

- Isaac Lab project documentation, "Container Deployment." <https://isaac-sim.github.io/IsaacLab/main/source/deployment/docker.html>
- NVIDIA Isaac Sim Dockerfiles. <https://github.com/NVIDIA-Omniverse/IsaacSim-dockerfiles>
- NVIDIA NGC catalog, Isaac Lab images. <https://catalog.ngc.nvidia.com/orgs/nvidia/containers/isaac-lab>
- NVIDIA Container Toolkit installation guide. <https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html>
- BuildKit documentation, `RUN --mount=type=cache`. <https://docs.docker.com/build/cache/optimize/>
