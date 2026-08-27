---
search:
  exclude: true
---
# 샌드박스 클라이언트

이 페이지에서 샌드박스 작업을 실행할 위치를 선택합니다. 대부분의 경우 `SandboxAgent` 정의는 그대로 유지하고, [`SandboxRunConfig`][agents.run_config.SandboxRunConfig]에서 샌드박스 클라이언트와 클라이언트별 옵션만 변경합니다.

!!! warning "베타 기능"

    샌드박스 에이전트는 베타 버전입니다. 정식 출시 전까지 API 세부 정보, 기본값, 지원 기능이 변경될 수 있으며, 시간이 지남에 따라 더 고급 기능이 추가될 예정입니다.

## 의사 결정 가이드 {#decision-guide}

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 목표 | 시작점 | 이유 |
| --- | --- | --- |
| macOS 또는 Linux에서 가장 빠른 로컬 반복 개발 | `UnixLocalSandboxClient` | 추가 설치가 필요 없으며, 로컬 파일 시스템에서 간단하게 개발할 수 있습니다. |
| 기본적인 컨테이너 격리 | `DockerSandboxClient` | 특정 이미지가 적용된 Docker 내부에서 작업을 실행합니다. |
| 호스티드 실행 또는 프로덕션 방식의 격리 | 호스티드 샌드박스 클라이언트 | 작업 공간 경계를 공급자가 관리하는 환경으로 이동합니다. |

</div>

## 로컬 클라이언트 {#local-clients}

대부분의 사용자는 다음 두 샌드박스 클라이언트 중 하나로 시작하는 것이 좋습니다.

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 클라이언트 | 설치 | 선택하는 경우 | 예제 |
| --- | --- | --- | --- |
| `UnixLocalSandboxClient` | 없음 | macOS 또는 Linux에서 가장 빠른 로컬 반복 개발이 필요한 경우입니다. 로컬 개발에 적합한 기본 옵션입니다. | [Unix 로컬 시작 예제](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/unix_local_runner.py) |
| `DockerSandboxClient` | `openai-agents[docker]` | 컨테이너 격리가 필요하거나 대상 환경을 로컬에서 재현하기 위해 특정 이미지를 사용하려는 경우입니다. | [Docker 시작 예제](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) |

</div>

Unix 로컬은 로컬 파일 시스템을 대상으로 개발을 시작하는 가장 쉬운 방법입니다. 더 강력한 환경 격리 또는 프로덕션 방식과의 동등성이 필요하면 Docker나 호스티드 공급자로 전환합니다.

`SandboxPathGrant.host_path`은 Docker에서만 사용할 수 있으며 호스트 경로를 컨테이너 내부의 다른 POSIX 경로에 매핑합니다. Unix 로컬은 동일 경로 허용만 지원합니다. 자세한 내용은 [매니페스트 경로 허용](guide.md#manifest)을 참조하세요.

Unix 로컬에서 Docker로 전환하려면 에이전트 정의는 그대로 유지하고 실행 구성만 변경합니다.

```python
from docker import from_env as docker_from_env

from agents.run import RunConfig
from agents.sandbox import SandboxRunConfig
from agents.sandbox.sandboxes.docker import DockerSandboxClient, DockerSandboxClientOptions

run_config = RunConfig(
    sandbox=SandboxRunConfig(
        client=DockerSandboxClient(docker_from_env()),
        options=DockerSandboxClientOptions(image="python:3.14-slim"),
    ),
)
```

컨테이너 격리가 필요하거나 샌드박스 이미지를 다른 환경에서 사용하는 이미지와 일치시키려는 경우 이 방법을 사용합니다. [examples/sandbox/docker/docker_runner.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py)를 참조하세요.

### Docker 네트워킹 비활성화 {#disable-docker-networking}

Docker 샌드박스에서 네트워크 액세스를 차단해야 하는 경우 `network_mode="none"`을 설정합니다.

```python
options = DockerSandboxClientOptions(
    image="python:3.14-slim",
    network_mode="none",
)
```

명시적으로 지원되는 유일한 네트워크 모드는 `"none"`입니다. Docker의 기본 동작을 유지하려면 `network_mode`을 생략합니다. 네트워크가 비활성화된 샌드박스는 포트를 노출할 수 없으므로 `network_mode="none"`과 비어 있지 않은 `exposed_ports` 튜플을 함께 사용하면 옵션 검증 중 실패합니다. 이 설정은 샌드박스 세션 상태에 저장되며, SDK가 해당 상태를 재개하는 동안 대체 컨테이너를 생성해야 하는 경우 다시 적용됩니다.

## 마운트 및 원격 스토리지 {#mounts-and-remote-storage}

마운트 항목은 노출할 스토리지를 설명하고, 마운트 전략은 샌드박스 백엔드가 해당 스토리지를 연결하는 방식을 설명합니다. 기본 제공 마운트 항목과 범용 전략은 `agents.sandbox.entries`에서 가져옵니다. 호스티드 공급자 전략은 `agents.extensions.sandbox` 또는 공급자별 확장 패키지에서 사용할 수 있습니다.

일반적인 마운트 옵션은 다음과 같습니다.

- `mount_path`: 스토리지가 샌드박스에 표시되는 위치입니다. 상대 경로는 매니페스트 루트를 기준으로 해석되며, 절대 경로는 그대로 사용됩니다.
- `read_only`: 기본값은 `True`입니다. 샌드박스에서 마운트된 스토리지에 변경 사항을 다시 기록해야 하는 경우에만 `False`을 설정합니다.
- `mount_strategy`: 필수입니다. 마운트 항목과 샌드박스 백엔드 모두에 맞는 전략을 사용합니다.

마운트는 임시 작업 공간 항목으로 취급됩니다. 스냅샷 및 지속성 흐름에서는 마운트된 원격 스토리지를 저장된 작업 공간으로 복사하는 대신 마운트된 경로를 분리하거나 건너뜁니다.

범용 로컬/컨테이너 전략은 다음과 같습니다.

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 전략 또는 패턴 | 사용하는 경우 | 참고 사항 |
| --- | --- | --- |
| `InContainerMountStrategy(pattern=RcloneMountPattern(...))` | 샌드박스 이미지에서 `rclone`을 실행할 수 있는 경우입니다. | S3, GCS, R2, Azure Blob 및 Box를 지원합니다. `RcloneMountPattern`는 `fuse` 모드 또는 `nfs` 모드로 실행할 수 있습니다. |
| `InContainerMountStrategy(pattern=MountpointMountPattern(...))` | 이미지에 `mount-s3`이 있으며 Mountpoint 방식의 S3 또는 S3 호환 액세스를 사용하려는 경우입니다. | `S3Mount` 및 `GCSMount`을 지원합니다. |
| `InContainerMountStrategy(pattern=FuseMountPattern(...))` | 이미지에 `blobfuse2` 및 FUSE 지원이 있는 경우입니다. | `AzureBlobMount`을 지원합니다. |
| `InContainerMountStrategy(pattern=S3FilesMountPattern(...))` | 이미지에 `mount.s3files`이 있으며 기존 S3 Files 마운트 대상에 연결할 수 있는 경우입니다. | `S3FilesMount`를 지원합니다. |
| `DockerVolumeMountStrategy(driver=...)` | 컨테이너가 시작되기 전에 Docker가 볼륨 드라이버 기반 마운트를 연결해야 하는 경우입니다. | Docker 전용입니다. S3, GCS, R2, Azure Blob 및 Box는 `rclone`을 통해 마운트할 수 있으며, S3와 GCS는 `mountpoint`을 통해서도 마운트할 수 있습니다. |

</div>

## 지원되는 호스티드 플랫폼 {#supported-hosted-platforms}

호스티드 환경이 필요한 경우 일반적으로 동일한 `SandboxAgent` 정의를 그대로 사용하고 [`SandboxRunConfig`][agents.run_config.SandboxRunConfig]에서 샌드박스 클라이언트만 변경합니다.

이 저장소의 체크아웃 대신 배포된 SDK를 사용하는 경우 일치하는 패키지 extra를 통해 샌드박스 클라이언트 종속성을 설치합니다.

저장소에 포함된 확장 예제의 공급자별 설정 참고 사항과 링크는 [examples/sandbox/extensions/README.md](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/README.md)를 참조하세요.

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 클라이언트 | 설치 | 예제 |
| --- | --- | --- |
| `BlaxelSandboxClient` | `openai-agents[blaxel]` | [Blaxel 실행 예제](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/blaxel_runner.py) |
| `CloudflareSandboxClient` | `openai-agents[cloudflare]` | [Cloudflare 실행 예제](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/cloudflare_runner.py) |
| `DaytonaSandboxClient` | `openai-agents[daytona]` | [Daytona 실행 예제](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/daytona/daytona_runner.py) |
| `E2BSandboxClient` | `openai-agents[e2b]` | [E2B 실행 예제](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/e2b_runner.py) |
| `ModalSandboxClient` | `openai-agents[modal]` | [Modal 실행 예제](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/modal_runner.py) |
| `RunloopSandboxClient` | `openai-agents[runloop]` | [Runloop 실행 예제](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/runloop/runner.py) |
| `VercelSandboxClient` | `openai-agents[vercel]` | [Vercel 실행 예제](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/vercel_runner.py) |

</div>

### Modal 샌드박스 크기 지정 {#size-modal-sandboxes}

새 Modal 샌드박스의 리소스를 요청하려면 `ModalSandboxClientOptions.cpu`와 `ModalSandboxClientOptions.memory`를 사용합니다. 단일 값은 해당 양을 요청합니다. 항목이 두 개인 `(request, limit)` 튜플에서는 첫 번째 항목을 요청값으로, 두 번째 항목을 제한값으로 사용합니다. 메모리 값의 단위는 MiB입니다.

```python
from agents.extensions.sandbox import ModalSandboxClientOptions

options = ModalSandboxClientOptions(
    app_name="agents-sandbox",
    cpu=(1.0, 4.0),
    memory=(2048, 8192),
)
```

생략된 각 리소스에 Modal의 기본값을 사용하려면 `cpu`, `memory` 또는 둘 다를 `None`으로 둡니다. 선택한 값은 샌드박스 세션 상태에 유지되므로 대체 샌드박스에서도 동일한 리소스 구성을 사용합니다.

호스티드 샌드박스 클라이언트는 공급자별 마운트 전략을 제공합니다. 스토리지 공급자에 가장 적합한 백엔드와 마운트 전략을 선택합니다.

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 백엔드 | 마운트 참고 사항 |
| --- | --- |
| Docker | `InContainerMountStrategy` 및 `DockerVolumeMountStrategy`과 같은 로컬 전략을 통해 `S3Mount`, `GCSMount`, `R2Mount`, `AzureBlobMount`, `BoxMount` 및 `S3FilesMount`를 지원합니다. |
| `ModalSandboxClient` | `ModalCloudBucketMountStrategy`를 `S3Mount`, `R2Mount` 및 HMAC 인증을 사용하는 `GCSMount`와 함께 사용하여 클라우드 버킷 마운트를 지원합니다. 인라인 자격 증명 또는 이름이 지정된 Modal Secret을 사용할 수 있습니다. |
| `CloudflareSandboxClient` | `CloudflareBucketMountStrategy`를 `S3Mount`, `R2Mount` 및 HMAC 인증을 사용하는 `GCSMount`과 함께 사용하여 버킷 마운트를 지원합니다. |
| `BlaxelSandboxClient` | `BlaxelCloudBucketMountStrategy`를 `S3Mount`, `R2Mount` 또는 `GCSMount` 항목과 함께 사용하여 클라우드 버킷 마운트를 지원합니다. 또한 `BlaxelDriveMount` 및 `BlaxelDriveMountStrategy`를 통해 영구 Blaxel Drives를 지원하며, 둘 다 `agents.extensions.sandbox.blaxel`에서 사용할 수 있습니다. |
| `DaytonaSandboxClient` | `DaytonaCloudBucketMountStrategy`을 사용하여 `rclone`을 통한 클라우드 스토리지 마운트를 지원합니다. `S3Mount`, `GCSMount`, `R2Mount`, `AzureBlobMount` 및 `BoxMount`과 함께 사용합니다. |
| `E2BSandboxClient` | `E2BCloudBucketMountStrategy`을 사용하여 `rclone`를 통한 클라우드 스토리지 마운트를 지원합니다. `S3Mount`, `GCSMount`, `R2Mount`, `AzureBlobMount` 및 `BoxMount`과 함께 사용합니다. |
| `RunloopSandboxClient` | `RunloopCloudBucketMountStrategy`를 사용하여 `rclone`을 통한 클라우드 스토리지 마운트를 지원합니다. `S3Mount`, `GCSMount`, `R2Mount`, `AzureBlobMount` 및 `BoxMount`와 함께 사용합니다. |
| `VercelSandboxClient` | `VercelCloudBucketMountStrategy`을 `S3Mount` 항목과 함께 사용하여 생성 시점에만 S3 및 S3 호환 버킷 마운트를 지원합니다. 마운트된 세션은 재개할 수 없으며, 인라인 자격 증명을 사용하려면 `allow_s3_credential_exposure=True`이 필요합니다. |

</div>

마운트 표는 각 백엔드에서 실행할 수 있는 스토리지 유형을 설명합니다. 체크 표시는 모델이 제어하는 샌드박스 내부에서 실행되는 마운트 도우미의 자격 증명 경계를 우회하지 않으며, 모든 전략이 자격 증명 없이 작동할 수 있다는 의미도 아닙니다. Agents SDK는 선택한 도우미가 보호된 권한 없이 작동할 수 있는 경우에만 별도의 확인 없이 컨테이너 내부 마운트를 허용합니다. 보호된 권한이 필요한 마운트는 신뢰할 수 있는 애플리케이션 코드에서 정확한 마운트 경로의 노출을 명시적으로 확인하지 않는 한 샌드박스 또는 마운트 도우미를 시작하기 전에 거부됩니다.

자격 증명이 없는 `rclone` 마운트는 S3, GCS, R2 및 Azure Blob으로 제한됩니다. 컨테이너 내부 Box 마운트에는 비대화형 인증 소스와 해당 소스에 부합하는 확인이 필요합니다. 인라인 자격 증명이 구성되지 않은 경우에도 `blobfuse2`이 주변 Azure 권한을 탐지하므로 `FuseMountPattern`에는 광범위한 확인이 필요합니다. 마찬가지로 `mount.s3files`이 주변 IAM 권한을 사용하므로 `S3FilesMountPattern`에도 광범위한 확인이 필요합니다. 이러한 요구 사항은 Docker가 백엔드인 경우에도 적용됩니다. 아래 체크 표시는 해당 권한 경계가 충족된 후 Docker에서 마운트를 실행할 수 있음을 나타냅니다.

이름이 `"data"`인 마운트 항목의 경우 구성된 권한에 부합하는 확인에서 반환된 복사본 `Manifest`을 유지합니다.

```python
# Mount-scoped values such as inline access keys.
manifest = manifest.with_in_container_mount_credential_exposure_acknowledged("data")

# Broader authority such as managed or workload identity and external credential files.
manifest = manifest.with_in_container_mount_broad_credential_exposure_acknowledged("data")
```

확인이 필요한 모든 정확한 마운트 경로를 전달합니다. 두 권한 클래스를 모두 사용하는 마운트에는 두 확인이 모두 필요합니다. 확인은 런타임에만 적용되고 직렬화되지 않으며, 도우미가 자격 증명을 사용할 수 있는 범위를 마운트된 경로로 제한하지 않은 채 자격 증명을 수신하도록 허용합니다. 가능한 경우 외부 또는 공급자 네이티브 전략을 우선 사용하고, 그렇지 않으면 샌드박스 범위의 수명이 짧은 최소 권한 자격 증명을 사용합니다.

`VercelSandboxClientOptions(allow_s3_credential_exposure=True)`은 마운트 범위의 인라인 자격 증명을 사용하는 생성 시점 Vercel S3 마운트를 위한 호환성 옵션으로 유지됩니다. 이 옵션은 광범위한 자격 증명 권한을 승인하지 않습니다.

아래 표는 각 백엔드에서 직접 마운트할 수 있는 원격 스토리지 항목을 요약합니다.

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 백엔드 | AWS S3 | Cloudflare R2 | GCS | Azure Blob Storage | Box | S3 Files |
| --- | --- | --- | --- | --- | --- | --- |
| Docker | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `ModalSandboxClient` | ✓ | ✓ | ✓ | - | - | - |
| `CloudflareSandboxClient` | ✓ | ✓ | ✓ | - | - | - |
| `BlaxelSandboxClient` | ✓ | ✓ | ✓ | - | - | - |
| `DaytonaSandboxClient` | ✓ | ✓ | ✓ | ✓ | ✓ | - |
| `E2BSandboxClient` | ✓ | ✓ | ✓ | ✓ | ✓ | - |
| `RunloopSandboxClient` | ✓ | ✓ | ✓ | ✓ | ✓ | - |
| `VercelSandboxClient` | ✓ | - | - | - | - | - |

</div>

실행 가능한 예제를 더 살펴보려면 로컬, 코딩, 메모리, 핸드오프 및 에이전트 구성 패턴은 [examples/sandbox/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox)에서, 호스티드 샌드박스 클라이언트는 [examples/sandbox/extensions/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox/extensions)에서 확인하세요.