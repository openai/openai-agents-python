---
search:
  exclude: true
---
# サンドボックスクライアント

このページでは、サンドボックスでの作業をどこで実行するかを選択します。ほとんどの場合、`SandboxAgent` の定義は同じままにし、サンドボックスクライアントとクライアント固有のオプションを [`SandboxRunConfig`][agents.run_config.SandboxRunConfig] で変更します。

!!! warning "ベータ機能"

    サンドボックスエージェントはベータ版です。一般提供までに API の詳細、デフォルト値、サポートされる機能が変更される可能性があります。また、時間とともにより高度な機能が追加される見込みです。

## 判断ガイド

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 目的 | まず使うもの | 理由 |
| --- | --- | --- |
| macOS または Linux での最速のローカル反復 | `UnixLocalSandboxClient` | 追加インストール不要で、シンプルなローカルファイルシステム開発ができます。 |
| 基本的なコンテナ分離 | `DockerSandboxClient` | 特定のイメージを使って Docker 内で作業を実行します。 |
| ホスト型実行または本番環境スタイルの分離 | ホスト型サンドボックスクライアント | ワークスペース境界をプロバイダー管理環境へ移します。 |

</div>

## ローカルクライアント

ほとんどのユーザーは、これら 2 つのサンドボックスクライアントのいずれかから始めることをおすすめします。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| クライアント | インストール | 選ぶ場面 | 例 |
| --- | --- | --- | --- |
| `UnixLocalSandboxClient` | なし | macOS または Linux で最速のローカル反復が必要な場合。ローカル開発の既定として適しています。 | [Unix-local スターター](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/unix_local_runner.py) |
| `DockerSandboxClient` | `openai-agents[docker]` | コンテナ分離、またはローカルで同等性を保つための特定のイメージが必要な場合。 | [Docker スターター](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) |

</div>

Unix-local は、ローカルファイルシステムに対して開発を始める最も簡単な方法です。より強い環境分離や本番環境スタイルの同等性が必要になったら、Docker またはホスト型プロバイダーへ移行してください。

Unix-local から Docker に切り替えるには、エージェント定義は同じままにして、実行設定だけを変更します。

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

コンテナ分離またはイメージの同等性が必要な場合に使用してください。[examples/sandbox/docker/docker_runner.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) を参照してください。

## マウントとリモートストレージ

マウントエントリーはどのストレージを公開するかを表し、マウント戦略はサンドボックスバックエンドがそのストレージをどのようにアタッチするかを表します。組み込みのマウントエントリーと汎用戦略は `agents.sandbox.entries` からインポートします。ホスト型プロバイダーの戦略は `agents.extensions.sandbox` またはプロバイダー固有の拡張パッケージから利用できます。

一般的なマウントオプション:

- `mount_path`: ストレージがサンドボックス内で表示される場所です。相対パスはマニフェストルート配下で解決され、絶対パスはそのまま使用されます。
- `read_only`: 既定は `True` です。サンドボックスがマウントされたストレージへ書き戻す必要がある場合にのみ `False` に設定してください。
- `mount_strategy`: 必須です。マウントエントリーとサンドボックスバックエンドの両方に合う戦略を使用してください。

マウントは一時的なワークスペースエントリーとして扱われます。スナップショットと永続化のフローでは、マウントされたリモートストレージを保存済みワークスペースへコピーするのではなく、マウントされたパスをデタッチするかスキップします。

汎用ローカル / コンテナ戦略:

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 戦略またはパターン | 使用する場面 | 備考 |
| --- | --- | --- |
| `InContainerMountStrategy(pattern=RcloneMountPattern(...))` | サンドボックスイメージで `rclone` を実行できる場合。 | S3、GCS、R2、Azure Blob、Box をサポートします。`RcloneMountPattern` は `fuse` モードまたは `nfs` モードで実行できます。 |
| `InContainerMountStrategy(pattern=MountpointMountPattern(...))` | イメージに `mount-s3` があり、Mountpoint スタイルの S3 または S3 互換アクセスが必要な場合。 | `S3Mount` と `GCSMount` をサポートします。 |
| `InContainerMountStrategy(pattern=FuseMountPattern(...))` | イメージに `blobfuse2` があり、FUSE サポートがある場合。 | `AzureBlobMount` をサポートします。 |
| `InContainerMountStrategy(pattern=S3FilesMountPattern(...))` | イメージに `mount.s3files` があり、既存の S3 Files マウントターゲットに到達できる場合。 | `S3FilesMount` をサポートします。 |
| `DockerVolumeMountStrategy(driver=...)` | Docker がコンテナ起動前にボリュームドライバー対応のマウントをアタッチする必要がある場合。 | Docker のみです。`rclone` は S3、GCS、R2、Azure Blob、Box をサポートし、`mountpoint` は S3 と GCS もサポートします。 |

</div>

## サポートされるホスト型プラットフォーム

ホスト型環境が必要な場合、通常は同じ `SandboxAgent` 定義をそのまま引き継ぎ、[`SandboxRunConfig`][agents.run_config.SandboxRunConfig] でサンドボックスクライアントだけを変更します。

このリポジトリのチェックアウトではなく公開されている SDK を使用している場合は、対応するパッケージ extra を通じてサンドボックスクライアントの依存関係をインストールしてください。

プロバイダー固有のセットアップメモと、チェックイン済みの拡張コード例へのリンクについては、[examples/sandbox/extensions/README.md](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/README.md) を参照してください。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| クライアント | インストール | 例 |
| --- | --- | --- |
| `BlaxelSandboxClient` | `openai-agents[blaxel]` | [Blaxel ランナー](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/blaxel_runner.py) |
| `CloudflareSandboxClient` | `openai-agents[cloudflare]` | [Cloudflare ランナー](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/cloudflare_runner.py) |
| `DaytonaSandboxClient` | `openai-agents[daytona]` | [Daytona ランナー](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/daytona/daytona_runner.py) |
| `E2BSandboxClient` | `openai-agents[e2b]` | [E2B ランナー](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/e2b_runner.py) |
| `ModalSandboxClient` | `openai-agents[modal]` | [Modal ランナー](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/modal_runner.py) |
| `RunloopSandboxClient` | `openai-agents[runloop]` | [Runloop ランナー](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/runloop/runner.py) |
| `VercelSandboxClient` | `openai-agents[vercel]` | [Vercel ランナー](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/vercel_runner.py) |

</div>

ホスト型サンドボックスクライアントは、プロバイダー固有のマウント戦略を公開します。ストレージプロバイダーに最も合うバックエンドとマウント戦略を選択してください。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| バックエンド | マウントに関する注記 |
| --- | --- |
| Docker | `InContainerMountStrategy` や `DockerVolumeMountStrategy` などのローカル戦略で、`S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`、`BoxMount`、`S3FilesMount` をサポートします。 |
| `ModalSandboxClient` | `S3Mount`、`R2Mount`、HMAC 認証済みの `GCSMount` で、`ModalCloudBucketMountStrategy` による Modal のクラウドバケットマウントをサポートします。インライン認証情報、または名前付きの Modal Secret を使用できます。 |
| `CloudflareSandboxClient` | `S3Mount`、`R2Mount`、HMAC 認証済みの `GCSMount` で、`CloudflareBucketMountStrategy` による Cloudflare バケットマウントをサポートします。 |
| `BlaxelSandboxClient` | `S3Mount`、`R2Mount`、`GCSMount` で、`BlaxelCloudBucketMountStrategy` によるクラウドバケットマウントをサポートします。`agents.extensions.sandbox.blaxel` の `BlaxelDriveMount` と `BlaxelDriveMountStrategy` による永続的な Blaxel Drives もサポートします。 |
| `DaytonaSandboxClient` | `DaytonaCloudBucketMountStrategy` による `rclone` ベースのクラウドストレージマウントをサポートします。`S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`、`BoxMount` と組み合わせて使用してください。 |
| `E2BSandboxClient` | `E2BCloudBucketMountStrategy` による `rclone` ベースのクラウドストレージマウントをサポートします。`S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`、`BoxMount` と組み合わせて使用してください。 |
| `RunloopSandboxClient` | `RunloopCloudBucketMountStrategy` による `rclone` ベースのクラウドストレージマウントをサポートします。`S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`、`BoxMount` と組み合わせて使用してください。 |
| `VercelSandboxClient` | 現時点ではホスト型固有のマウント戦略は公開されていません。代わりにマニフェストファイル、リポジトリ、またはその他のワークスペース入力を使用してください。 |

</div>

以下の表は、各バックエンドが直接マウントできるリモートストレージエントリーをまとめたものです。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| バックエンド | AWS S3 | Cloudflare R2 | GCS | Azure Blob Storage | Box | S3 Files |
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

実行可能なコード例をさらに見るには、ローカル、コーディング、メモリ、ハンドオフ、エージェント合成パターンについては [examples/sandbox/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox) を、ホスト型サンドボックスクライアントについては [examples/sandbox/extensions/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox/extensions) を参照してください。