---
search:
  exclude: true
---
# 샌드박스 클라이언트

이 페이지를 사용하여 샌드박스 작업을 실행할 위치를 선택합니다. 대부분의 경우 `SandboxAgent` 정의는 그대로 유지하고 [`SandboxRunConfig`][agents.run_config.SandboxRunConfig]에서 샌드박스 클라이언트와 클라이언트별 옵션만 변경합니다.

!!! warning "베타 기능"

    샌드박스 에이전트는 베타 버전입니다. 정식 출시 전까지 API의 세부 사항, 기본값, 지원 기능이 변경될 수 있으며, 시간이 지나면서 더 고급 기능이 추가될 수 있습니다.

## 선택 가이드 {#decision-guide}

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 목표 | 시작 옵션 | 이유 |
| --- | --- | --- |
| macOS 또는 Linux에서 신뢰할 수 있는 로컬 개발 | `UnixLocalSandboxClient` | 추가 설치가 필요 없으며, 명령이 로컬 호스트 프로세스로 실행됩니다. |
| 기본적인 컨테이너 격리 | `DockerSandboxClient` | 특정 이미지를 사용하는 Docker 내부에서 작업을 실행합니다. |
| 호스티드 실행 또는 프로덕션 수준의 격리 | 호스티드 샌드박스 클라이언트 | 작업 공간 경계를 공급자가 관리하는 환경으로 이동합니다. |

</div>

!!! warning "유닉스 로컬 실행 제한"

    `UnixLocalSandboxClient`는 명령을 로컬 호스트 프로세스로 실행합니다. Linux에서 이 백엔드는 OS 수준의 격리를 추가하지 않습니다. 명령은 호스트 프로세스와 외부 격리에서 허용하는 파일 및 네트워크 리소스에 접근할 수 있습니다. 작업 공간 디렉터리, `HOME` 또는 `cwd`은 이러한 접근을 제한하지 않습니다.

    macOS에서 이 백엔드는 `sandbox-exec`을 사용하여 파일 시스템 제한을 적용합니다. 이러한 제한은 네트워크 격리나 컨테이너와 동일한 수준의 경계를 제공하지 않습니다.

    유닉스 로컬은 신뢰할 수 있는 로컬 개발이나 외부에서 격리된 환경 내에서 사용합니다. 신뢰할 수 없는 입력의 영향을 받는 명령을 포함하여 신뢰할 수 없는 명령에는 적절하게 구성된 Docker 또는 호스티드 샌드박스를 선택하거나 외부 격리를 제공해야 합니다. 워크로드에 맞게 선택한 환경의 권한, 마운트, 자격 증명, 네트워크 접근을 검토하세요.

## 로컬 클라이언트 {#local-clients}

대부분의 사용자는 다음 두 샌드박스 클라이언트 중 하나로 시작하는 것이 좋습니다.

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 클라이언트 | 설치 | 선택하는 경우 | 예제 |
| --- | --- | --- | --- |
| `UnixLocalSandboxClient` | 없음 | macOS 또는 Linux에서 신뢰할 수 있는 로컬 개발을 수행하거나 외부 격리 내에서 실행하는 경우 | [유닉스 로컬 시작 예제](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/unix_local_runner.py) |
| `DockerSandboxClient` | `openai-agents[docker]` | 컨테이너 격리가 필요하거나 대상 환경을 로컬에서 재현하기 위해 특정 이미지를 사용하려는 경우 | [Docker 시작 예제](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) |

</div>

유닉스 로컬은 컨테이너 없이 로컬 작업 공간을 제공합니다. 해당 백엔드가 제공하는 격리 경계나 다른 환경과 일치하는 이미지가 필요한 경우 Docker 또는 호스티드 공급자를 선택합니다.

`SandboxPathGrant.host_path`은 Docker에서만 사용할 수 있으며 호스트 경로를 컨테이너 내부의 다른 POSIX 경로에 매핑합니다. 유닉스 로컬은 동일한 경로에 대한 권한 부여만 지원합니다. 자세한 내용은 [매니페스트 경로 권한 부여](guide.md#manifest)를 참조하세요.

### 유닉스 로컬 세션의 호스트 환경 상속 제한 {#limit-host-environment-inheritance-for-unix-local-sessions}

기본적으로 `UnixLocalSandboxClient`는 모든 명령 환경을 전체 호스트 프로세스 환경으로 시작합니다. 대신 보수적인 호스트 변수 허용 목록만 전달하려면 `inherit_host_environment=False`을 설정합니다.

```python
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

client = UnixLocalSandboxClient(
    inherit_host_environment=False,
    host_environment_allowlist={"PATH", "LANG", "SSL_CERT_FILE"},
)
```

`inherit_host_environment=False`이고 `host_environment_allowlist`이 생략된 경우 SDK는 `PATH`, `LANG`, `LC_ALL`, `LC_COLLATE`, `LC_CTYPE`, `LC_MESSAGES`, `LC_MONETARY`, `LC_NUMERIC`, `LC_TIME`, `TZ`, `TERM`, `TMPDIR`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`, `UV_PYTHON`, `NO_COLOR`, `FORCE_COLOR`, `CI`을 허용합니다. 이 기본 허용 목록을 대체하려면 사용자 지정 컬렉션을 전달합니다. 사용자 지정 허용 목록에는 `inherit_host_environment=False`이 필요합니다.

`Manifest.environment`의 값은 호스트 필터링 후 적용되며 상속된 값을 재정의합니다. 유닉스 로컬 명령은 항상 작업 공간 루트를 `HOME`로 전달받습니다. 상속 정책은 직렬화된 세션 상태가 아니라 현재 클라이언트에 속하므로 `create(...)`와 `resume(...)`은 해당 작업을 수행하는 클라이언트의 정책을 적용합니다.

이 옵션은 상속되는 환경 변수만 필터링하며 OS 수준의 격리를 추가하지 않습니다. 위에서 설명한 유닉스 로컬 실행 제한은 계속 적용됩니다.

유닉스 로컬에서 Docker로 전환하려면 에이전트 정의는 그대로 유지하고 실행 구성만 변경합니다.

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

컨테이너 격리가 필요하거나 샌드박스 이미지를 다른 환경에서 사용하는 이미지와 일치시키려는 경우 이 방식을 사용합니다. [examples/sandbox/docker/docker_runner.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py)를 참조하세요.

### Docker 네트워크 비활성화 {#disable-docker-networking}

Docker 샌드박스의 네트워크 접근을 차단해야 하는 경우 `network_mode="none"`을 설정합니다.

```python
options = DockerSandboxClientOptions(
    image="python:3.14-slim",
    network_mode="none",
)
```

명시적으로 지원되는 유일한 네트워크 모드는 `"none"`입니다. Docker의 기본 동작을 유지하려면 `network_mode`을 생략합니다. 네트워크가 비활성화된 샌드박스는 포트를 노출할 수 없으므로 `network_mode="none"`를 비어 있지 않은 `exposed_ports` 튜플과 함께 사용하면 옵션 검증 중 실패합니다. 이 설정은 샌드박스 세션 상태에 저장되며, SDK가 해당 상태를 재개하는 동안 대체 컨테이너를 생성해야 하는 경우 다시 적용됩니다.

### Docker 컨테이너 레이블 지정 {#label-docker-containers}

애플리케이션에서 샌드박스 세션을 위해 생성된 Docker 컨테이너를 식별하거나 관리해야 하는 경우 `labels`을 설정합니다.

```python
options = DockerSandboxClientOptions(
    image="python:3.14-slim",
    labels={
        "com.example.owner": "agents-sdk",
        "com.example.environment": "development",
    },
)
```

SDK는 컨테이너를 생성할 때 이러한 키-값 쌍을 Docker에 전달하고 [`DockerSandboxSessionState`][agents.sandbox.sandboxes.docker.DockerSandboxSessionState]에 저장합니다. 재개된 세션이 기존 컨테이너에 다시 연결되면 SDK는 저장된 모든 레이블이 여전히 예상 값을 갖는지 확인하고, 레이블이 일치하지 않으면 `ValueError`을 발생시킵니다. SDK가 저장된 상태에서 대체 컨테이너를 생성할 때는 저장된 레이블을 다시 적용합니다.

## 마운트 및 원격 스토리지 {#mounts-and-remote-storage}

마운트 항목은 노출할 스토리지를 설명하고, 마운트 전략은 샌드박스 백엔드가 해당 스토리지를 연결하는 방법을 설명합니다. 기본 제공 마운트 항목과 일반 전략은 `agents.sandbox.entries`에서 가져옵니다. 호스티드 공급자 전략은 `agents.extensions.sandbox` 또는 공급자별 확장 패키지에서 사용할 수 있습니다.

일반적인 마운트 옵션은 다음과 같습니다.

- `mount_path`: 샌드박스에서 스토리지가 표시되는 위치입니다. 상대 경로는 매니페스트 루트를 기준으로 해석되고 절대 경로는 그대로 사용됩니다.
- `read_only`: 기본값은 `True`입니다. 샌드박스에서 마운트된 스토리지에 변경 사항을 다시 기록해야 하는 경우에만 `False`를 설정합니다.
- `mount_strategy`: 필수입니다. 마운트 항목과 샌드박스 백엔드 모두에 맞는 전략을 사용합니다.

마운트는 임시 작업 공간 항목으로 취급됩니다. 스냅샷 및 영속성 처리 과정에서는 마운트된 원격 스토리지를 저장된 작업 공간에 복사하지 않고 마운트된 경로를 분리하거나 건너뜁니다.

일반 로컬/컨테이너 전략은 다음과 같습니다.

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 전략 또는 패턴 | 사용하는 경우 | 참고 |
| --- | --- | --- |
| `InContainerMountStrategy(pattern=RcloneMountPattern(...))` | 샌드박스 이미지에서 `rclone`을 실행할 수 있는 경우 | S3, GCS, R2, Azure Blob, Box를 지원합니다. `RcloneMountPattern`은 `fuse` 모드 또는 `nfs` 모드로 실행할 수 있습니다. |
| `InContainerMountStrategy(pattern=MountpointMountPattern(...))` | 이미지에 `mount-s3`가 있고 Mountpoint 방식의 S3 또는 S3 호환 접근을 사용하려는 경우 | `S3Mount`과 `GCSMount`를 지원합니다. |
| `InContainerMountStrategy(pattern=FuseMountPattern(...))` | 이미지에 `blobfuse2`과 FUSE 지원이 있는 경우 | `AzureBlobMount`을 지원합니다. |
| `InContainerMountStrategy(pattern=S3FilesMountPattern(...))` | 이미지에 `mount.s3files`가 있고 기존 S3 Files 마운트 대상에 접근할 수 있는 경우 | `S3FilesMount`을 지원합니다. |
| `DockerVolumeMountStrategy(driver=...)` | 컨테이너가 시작되기 전에 Docker에서 볼륨 드라이버 기반 마운트를 연결해야 하는 경우 | Docker 전용입니다. S3, GCS, R2, Azure Blob, Box는 `rclone`를 통해 마운트할 수 있고, S3와 GCS는 `mountpoint`을 통해서도 마운트할 수 있습니다. |

</div>

## 지원되는 호스티드 플랫폼 {#supported-hosted-platforms}

호스티드 환경이 필요한 경우 일반적으로 동일한 `SandboxAgent` 정의를 그대로 사용하고 [`SandboxRunConfig`][agents.run_config.SandboxRunConfig]에서 샌드박스 클라이언트만 변경합니다.

이 저장소 체크아웃 대신 배포된 SDK를 사용하는 경우 일치하는 패키지 extra를 통해 샌드박스 클라이언트 종속성을 설치합니다.

공급자별 설정 참고 사항과 저장소에 포함된 확장 예제 링크는 [examples/sandbox/extensions/README.md](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/README.md)를 참조하세요.

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

새 Modal 샌드박스의 리소스를 요청하려면 `ModalSandboxClientOptions.cpu`과 `ModalSandboxClientOptions.memory`을 사용합니다. 단일 값은 해당 수량을 요청합니다. 항목이 두 개인 `(request, limit)` 튜플은 첫 번째 항목을 요청값으로, 두 번째 항목을 제한값으로 사용합니다. 메모리 값의 단위는 MiB입니다.

```python
from agents.extensions.sandbox import ModalSandboxClientOptions

options = ModalSandboxClientOptions(
    app_name="agents-sandbox",
    cpu=(1.0, 4.0),
    memory=(2048, 8192),
)
```

생략한 각 리소스에 Modal의 기본값을 사용하려면 `cpu`, `memory` 또는 둘 다를 `None`로 둡니다. 선택한 값은 샌드박스 세션 상태에 보존되므로 대체 샌드박스에서도 동일한 리소스 구성을 사용합니다.

호스티드 샌드박스 클라이언트는 공급자별 마운트 전략을 제공합니다. 스토리지 공급자에 가장 적합한 백엔드와 마운트 전략을 선택합니다.

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 백엔드 | 마운트 참고 사항 |
| --- | --- |
| Docker | `InContainerMountStrategy`, `DockerVolumeMountStrategy` 같은 로컬 전략과 함께 `S3Mount`, `GCSMount`, `R2Mount`, `AzureBlobMount`, `BoxMount`, `S3FilesMount`을 지원합니다. |
| `ModalSandboxClient` | `S3Mount`, `R2Mount`, HMAC 인증을 사용하는 `GCSMount`과 함께 `ModalCloudBucketMountStrategy`를 사용하여 클라우드 버킷 마운트를 지원합니다. 인라인 자격 증명이나 이름이 지정된 Modal Secret을 사용할 수 있습니다. |
| `CloudflareSandboxClient` | `S3Mount`, `R2Mount`, HMAC 인증을 사용하는 `GCSMount`과 함께 `CloudflareBucketMountStrategy`을 사용하여 버킷 마운트를 지원합니다. |
| `BlaxelSandboxClient` | `BlaxelCloudBucketMountStrategy`를 `S3Mount`, `R2Mount` 또는 `GCSMount` 항목과 함께 사용하여 클라우드 버킷 마운트를 지원합니다. 또한 `agents.extensions.sandbox.blaxel`에서 모두 제공되는 `BlaxelDriveMount`와 `BlaxelDriveMountStrategy`을 사용하여 영구 Blaxel Drive를 지원합니다. |
| `DaytonaSandboxClient` | `DaytonaCloudBucketMountStrategy`를 사용하여 `rclone`을 통한 클라우드 스토리지 마운트를 지원합니다. `S3Mount`, `GCSMount`, `R2Mount`, `AzureBlobMount`, `BoxMount`와 함께 사용합니다. |
| `E2BSandboxClient` | `E2BCloudBucketMountStrategy`를 사용하여 `rclone`을 통한 클라우드 스토리지 마운트를 지원합니다. `S3Mount`, `GCSMount`, `R2Mount`, `AzureBlobMount`, `BoxMount`과 함께 사용합니다. |
| `RunloopSandboxClient` | `RunloopCloudBucketMountStrategy`을 사용하여 `rclone`를 통한 클라우드 스토리지 마운트를 지원합니다. `S3Mount`, `GCSMount`, `R2Mount`, `AzureBlobMount`, `BoxMount`와 함께 사용합니다. |
| `VercelSandboxClient` | `VercelCloudBucketMountStrategy`을 `S3Mount` 항목과 함께 사용하여 생성 시점에만 S3 및 S3 호환 버킷 마운트를 지원합니다. 마운트된 세션은 재개할 수 없으며 인라인 자격 증명에는 `allow_s3_credential_exposure=True`가 필요합니다. |

</div>

마운트 표는 각 백엔드에서 실행할 수 있는 스토리지 유형을 설명합니다. 확인 표시는 모델이 제어하는 샌드박스 내부에서 실행되는 마운트 헬퍼의 자격 증명 경계를 우회하지 않으며, 모든 전략이 자격 증명 없이 작동할 수 있다는 의미도 아닙니다. Agents SDK는 선택한 헬퍼가 보호된 권한 없이 작동할 수 있는 경우에만 확인 절차 없이 컨테이너 내부 마운트를 허용합니다. 보호된 권한이 필요한 마운트의 경우, 신뢰할 수 있는 애플리케이션 코드가 정확한 마운트 경로에 대한 노출을 명시적으로 확인하지 않으면 샌드박스 또는 마운트 헬퍼를 시작하기 전에 거부합니다.

자격 증명이 없는 `rclone` 마운트는 S3, GCS, R2, Azure Blob으로 제한됩니다. 컨테이너 내부 Box 마운트에는 비대화형 인증 소스와 해당 소스에 맞는 확인이 필요합니다. `FuseMountPattern`에는 인라인 자격 증명이 구성되지 않은 경우에도 `blobfuse2`가 환경의 Azure 권한을 탐색하므로 광범위한 확인이 필요합니다. 마찬가지로 `S3FilesMountPattern`에도 `mount.s3files`가 환경의 IAM 권한을 사용하므로 광범위한 확인이 필요합니다. 이러한 요구 사항은 Docker가 백엔드인 경우에도 적용됩니다. 아래의 확인 표시는 해당 권한 경계가 충족된 후 Docker가 마운트를 실행할 수 있음을 나타냅니다.

이름이 `"data"`인 마운트 항목의 경우 구성된 권한과 일치하는 확인에서 반환된 `Manifest`의 복사본을 보관합니다.

```python
# Mount-scoped values such as inline access keys.
manifest = manifest.with_in_container_mount_credential_exposure_acknowledged("data")

# Broader authority such as managed or workload identity and external credential files.
manifest = manifest.with_in_container_mount_broad_credential_exposure_acknowledged("data")
```

확인이 필요한 모든 정확한 마운트 경로를 전달합니다. 두 권한 클래스를 모두 사용하는 마운트에는 두 확인이 모두 필요합니다. 확인은 런타임에만 적용되고 직렬화되지 않으며, 헬퍼가 자격 증명을 사용할 수 있는 범위를 마운트된 경로로 제한하지 않은 채 자격 증명을 받을 수 있도록 허용합니다. 가능한 경우 외부 전략이나 공급자 네이티브 전략을 우선 사용하고, 그렇지 않으면 샌드박스 범위의 수명이 짧은 최소 권한 자격 증명을 사용합니다.

`VercelSandboxClientOptions(allow_s3_credential_exposure=True)`은 인라인 마운트 범위 자격 증명을 사용하는 생성 시점 Vercel S3 마운트를 위한 호환성 옵션으로 유지됩니다. 이 옵션은 광범위한 자격 증명 권한을 승인하지 않습니다.

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

더 많은 실행 가능한 예제를 보려면 로컬, 코딩, 메모리, 핸드오프, 에이전트 구성 패턴은 [examples/sandbox/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox)를, 호스티드 샌드박스 클라이언트는 [examples/sandbox/extensions/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox/extensions)를 살펴보세요.