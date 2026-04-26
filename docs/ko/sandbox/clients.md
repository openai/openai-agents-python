---
search:
  exclude: true
---
# 샌드박스 클라이언트

이 페이지를 사용해 샌드박스 작업을 어디에서 실행할지 선택하세요. 대부분의 경우 `SandboxAgent` 정의는 동일하게 유지되고, 샌드박스 클라이언트와 클라이언트별 옵션만 [`SandboxRunConfig`][agents.run_config.SandboxRunConfig]에서 변경됩니다.

!!! warning "베타 기능"

    샌드박스 에이전트는 베타입니다. 정식 출시 전까지 API의 세부 사항, 기본값, 지원 기능이 변경될 수 있으며, 시간이 지나면서 더 고급 기능이 추가될 수 있습니다.

## 선택 가이드

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 목표 | 시작점 | 이유 |
| --- | --- | --- |
| macOS 또는 Linux에서 가장 빠른 로컬 반복 작업 | `UnixLocalSandboxClient` | 추가 설치가 필요 없고, 로컬 파일시스템 개발이 간단합니다. |
| 기본적인 컨테이너 격리 | `DockerSandboxClient` | 특정 이미지를 사용해 Docker 내부에서 작업을 실행합니다. |
| 호스팅 실행 또는 프로덕션 수준 격리 | 호스팅 샌드박스 클라이언트 | 작업공간 경계를 공급자가 관리하는 환경으로 옮깁니다. |

</div>

## 로컬 클라이언트

대부분의 사용자에게는 다음 두 가지 샌드박스 클라이언트 중 하나로 시작하는 것을 권장합니다.

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 클라이언트 | 설치 | 이런 경우 선택 | 예제 |
| --- | --- | --- | --- |
| `UnixLocalSandboxClient` | 없음 | macOS 또는 Linux에서 가장 빠른 로컬 반복 작업이 필요할 때. 로컬 개발에 좋은 기본값입니다. | [Unix-local 시작 예제](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/unix_local_runner.py) |
| `DockerSandboxClient` | `openai-agents[docker]` | 컨테이너 격리 또는 로컬 환경 일치를 위한 특정 이미지가 필요할 때 | [Docker 시작 예제](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) |

</div>

Unix-local은 로컬 파일시스템을 대상으로 개발을 시작하는 가장 쉬운 방법입니다. 더 강력한 환경 격리나 프로덕션 수준의 환경 일치가 필요하면 Docker 또는 호스팅 공급자로 이동하세요.

Unix-local에서 Docker로 전환하려면 에이전트 정의는 그대로 두고 실행 구성만 변경하면 됩니다.

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

컨테이너 격리 또는 이미지 일치가 필요할 때 이 방식을 사용하세요. [examples/sandbox/docker/docker_runner.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py)를 참고하세요.

## 마운트와 원격 스토리지

마운트 항목은 어떤 스토리지를 노출할지 설명하고, 마운트 전략은 샌드박스 백엔드가 해당 스토리지를 어떻게 연결할지 설명합니다. 내장 마운트 항목과 일반 전략은 `agents.sandbox.entries`에서 가져오세요. 호스팅 공급자 전략은 `agents.extensions.sandbox` 또는 공급자별 확장 패키지에서 사용할 수 있습니다.

일반적인 마운트 옵션:

- `mount_path`: 샌드박스에서 스토리지가 나타나는 위치입니다. 상대 경로는 매니페스트 루트 아래에서 해석되고, 절대 경로는 그대로 사용됩니다.
- `read_only`: 기본값은 `True`입니다. 샌드박스가 마운트된 스토리지에 다시 써야 하는 경우에만 `False`로 설정하세요.
- `mount_strategy`: 필수입니다. 마운트 항목과 샌드박스 백엔드 모두에 맞는 전략을 사용하세요.

마운트는 일시적인 작업공간 항목으로 처리됩니다. 스냅샷 및 영속화 흐름에서는 마운트된 원격 스토리지를 저장된 작업공간에 복사하는 대신, 마운트된 경로를 분리하거나 건너뜁니다.

일반 로컬/컨테이너 전략:

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 전략 또는 패턴 | 사용 시점 | 참고 |
| --- | --- | --- |
| `InContainerMountStrategy(pattern=RcloneMountPattern(...))` | 샌드박스 이미지에서 `rclone`을 실행할 수 있을 때 | S3, GCS, R2, Azure Blob, Box를 지원합니다. `RcloneMountPattern`은 `fuse` 모드 또는 `nfs` 모드로 실행할 수 있습니다. |
| `InContainerMountStrategy(pattern=MountpointMountPattern(...))` | 이미지에 `mount-s3`가 있고 Mountpoint 방식의 S3 또는 S3 호환 액세스를 원할 때 | `S3Mount`와 `GCSMount`를 지원합니다. |
| `InContainerMountStrategy(pattern=FuseMountPattern(...))` | 이미지에 `blobfuse2`와 FUSE 지원이 있을 때 | `AzureBlobMount`를 지원합니다. |
| `InContainerMountStrategy(pattern=S3FilesMountPattern(...))` | 이미지에 `mount.s3files`가 있고 기존 S3 Files 마운트 대상에 접근할 수 있을 때 | `S3FilesMount`를 지원합니다. |
| `DockerVolumeMountStrategy(driver=...)` | Docker가 컨테이너 시작 전에 볼륨 드라이버 기반 마운트를 연결해야 할 때 | Docker 전용입니다. S3, GCS, R2, Azure Blob, Box는 `rclone`을 지원하며, S3와 GCS는 `mountpoint`도 지원합니다. |

</div>

## 지원되는 호스팅 플랫폼

호스팅 환경이 필요할 때는 동일한 `SandboxAgent` 정의를 그대로 사용할 수 있으며, 일반적으로 [`SandboxRunConfig`][agents.run_config.SandboxRunConfig]에서 샌드박스 클라이언트만 변경하면 됩니다.

이 저장소 체크아웃이 아니라 배포된 SDK를 사용 중이라면, 일치하는 패키지 extra를 통해 샌드박스 클라이언트 의존성을 설치하세요.

공급자별 설정 참고 사항과 저장소에 포함된 확장 예제 링크는 [examples/sandbox/extensions/README.md](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/README.md)를 참고하세요.

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 클라이언트 | 설치 | 예제 |
| --- | --- | --- |
| `BlaxelSandboxClient` | `openai-agents[blaxel]` | [Blaxel 실행기](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/blaxel_runner.py) |
| `CloudflareSandboxClient` | `openai-agents[cloudflare]` | [Cloudflare 실행기](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/cloudflare_runner.py) |
| `DaytonaSandboxClient` | `openai-agents[daytona]` | [Daytona 실행기](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/daytona/daytona_runner.py) |
| `E2BSandboxClient` | `openai-agents[e2b]` | [E2B 실행기](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/e2b_runner.py) |
| `ModalSandboxClient` | `openai-agents[modal]` | [Modal 실행기](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/modal_runner.py) |
| `RunloopSandboxClient` | `openai-agents[runloop]` | [Runloop 실행기](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/runloop/runner.py) |
| `VercelSandboxClient` | `openai-agents[vercel]` | [Vercel 실행기](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/vercel_runner.py) |

</div>

호스팅 샌드박스 클라이언트는 공급자별 마운트 전략을 제공합니다. 스토리지 공급자에 가장 적합한 백엔드와 마운트 전략을 선택하세요.

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 백엔드 | 마운트 참고 사항 |
| --- | --- |
| Docker | `InContainerMountStrategy` 및 `DockerVolumeMountStrategy`와 같은 로컬 전략을 사용해 `S3Mount`, `GCSMount`, `R2Mount`, `AzureBlobMount`, `BoxMount`, `S3FilesMount`를 지원합니다. |
| `ModalSandboxClient` | `S3Mount`, `R2Mount`, HMAC 인증된 `GCSMount`에서 `ModalCloudBucketMountStrategy`를 사용한 Modal 클라우드 버킷 마운트를 지원합니다. 인라인 자격 증명 또는 이름 있는 Modal Secret을 사용할 수 있습니다. |
| `CloudflareSandboxClient` | `S3Mount`, `R2Mount`, HMAC 인증된 `GCSMount`에서 `CloudflareBucketMountStrategy`를 사용한 Cloudflare 버킷 마운트를 지원합니다. |
| `BlaxelSandboxClient` | `S3Mount`, `R2Mount`, `GCSMount`에서 `BlaxelCloudBucketMountStrategy`를 사용한 클라우드 버킷 마운트를 지원합니다. 또한 `agents.extensions.sandbox.blaxel`의 `BlaxelDriveMount` 및 `BlaxelDriveMountStrategy`를 사용한 영구 Blaxel Drive도 지원합니다. |
| `DaytonaSandboxClient` | `DaytonaCloudBucketMountStrategy`를 사용한 rclone 기반 클라우드 스토리지 마운트를 지원합니다. `S3Mount`, `GCSMount`, `R2Mount`, `AzureBlobMount`, `BoxMount`와 함께 사용하세요. |
| `E2BSandboxClient` | `E2BCloudBucketMountStrategy`를 사용한 rclone 기반 클라우드 스토리지 마운트를 지원합니다. `S3Mount`, `GCSMount`, `R2Mount`, `AzureBlobMount`, `BoxMount`와 함께 사용하세요. |
| `RunloopSandboxClient` | `RunloopCloudBucketMountStrategy`를 사용한 rclone 기반 클라우드 스토리지 마운트를 지원합니다. `S3Mount`, `GCSMount`, `R2Mount`, `AzureBlobMount`, `BoxMount`와 함께 사용하세요. |
| `VercelSandboxClient` | 현재 호스팅 전용 마운트 전략이 노출되어 있지 않습니다. 대신 매니페스트 파일, 저장소 또는 기타 작업공간 입력을 사용하세요. |

</div>

아래 표는 각 백엔드가 어떤 원격 스토리지 항목을 직접 마운트할 수 있는지 요약합니다.

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
| `VercelSandboxClient` | - | - | - | - | - | - |

</div>

실행 가능한 예제를 더 보려면 로컬, 코딩, 메모리, 핸드오프, 에이전트 구성 패턴은 [examples/sandbox/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox)를, 호스팅 샌드박스 클라이언트는 [examples/sandbox/extensions/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox/extensions)를 살펴보세요.