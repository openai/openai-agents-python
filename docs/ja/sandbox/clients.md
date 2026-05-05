---
search:
  exclude: true
---
# Sandbox クライアント

このページでは、 sandbox の作業をどこで実行するかを選択します。ほとんどの場合、 `SandboxAgent` の定義は同じままで、 sandbox クライアントとクライアント固有のオプションのみが [`SandboxRunConfig`][agents.run_config.SandboxRunConfig] で変わります。

!!! warning "Beta 機能"

    Sandbox エージェントは beta です。一般提供前に API の詳細、デフォルト、対応機能が変更される可能性があり、時間の経過とともにより高度な機能も追加される予定です。

## 判断ガイド

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 目的 | まず使うもの | 理由 |
| --- | --- | --- |
| macOS または Linux で最速のローカル反復 | `UnixLocalSandboxClient` | 追加インストール不要で、シンプルなローカルファイルシステム開発ができます。 |
| 基本的なコンテナ分離 | `DockerSandboxClient` | 特定のイメージを使って Docker 内で作業を実行します。 |
| ホスト型実行または本番環境に近い分離 | ホスト型 sandbox クライアント | ワークスペースの境界をプロバイダー管理の環境に移します。 |

</div>

## ローカルクライアント

ほとんどのユーザーは、まず次の 2 つの sandbox クライアントのいずれかから始めてください。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| クライアント | インストール | 選ぶ場面 | 例 |
| --- | --- | --- | --- |
| `UnixLocalSandboxClient` | なし | macOS または Linux で最速にローカル反復したい場合。ローカル開発の良いデフォルトです。 | [Unix-local スターター](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/unix_local_runner.py) |
| `DockerSandboxClient` | `openai-agents[docker]` | コンテナ分離や、ローカルでの同等性のために特定のイメージが必要な場合。 | [Docker スターター](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) |

</div>

Unix-local は、ローカルファイルシステムを対象にした開発を始める最も簡単な方法です。より強い環境分離や本番環境に近い同等性が必要になったら、 Docker またはホスト型プロバイダーに移行してください。

Unix-local から Docker に切り替えるには、エージェント定義はそのままにして、 run config のみを変更します。

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

これは、コンテナ分離やイメージの同等性が必要な場合に使用します。[examples/sandbox/docker/docker_runner.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) を参照してください。

## マウントとリモートストレージ

mount エントリは公開するストレージを記述し、 mount 戦略は sandbox バックエンドがそのストレージをどのように接続するかを記述します。組み込みの mount エントリと汎用戦略は `agents.sandbox.entries` からインポートします。ホスト型プロバイダーの戦略は `agents.extensions.sandbox` またはプロバイダー固有の拡張パッケージから利用できます。

一般的な mount オプション:

- `mount_path`: sandbox 内でストレージが表示される場所です。相対パスは manifest ルート配下で解決され、絶対パスはそのまま使われます。
- `read_only`: デフォルトは `True` です。 sandbox からマウントされたストレージへ書き戻す必要がある場合にのみ `False` に設定してください。
- `mount_strategy`: 必須です。 mount エントリと sandbox バックエンドの両方に適合する戦略を使用してください。

mount は一時的なワークスペースエントリとして扱われます。スナップショットおよび永続化フローでは、マウントされたリモートストレージを保存済みワークスペースにコピーするのではなく、マウントされたパスを切り離すかスキップします。

汎用のローカル / コンテナ戦略:

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 戦略またはパターン | 使用する場面 | 注記 |
| --- | --- | --- |
| `InContainerMountStrategy(pattern=RcloneMountPattern(...))` | sandbox イメージで `rclone` を実行できる場合。 | S3 、 GCS 、 R2 、 Azure Blob 、 Box をサポートします。`RcloneMountPattern` は `fuse` モードまたは `nfs` モードで実行できます。 |
| `InContainerMountStrategy(pattern=MountpointMountPattern(...))` | イメージに `mount-s3` があり、 Mountpoint スタイルの S3 または S3 互換アクセスを使いたい場合。 | `S3Mount` と `GCSMount` をサポートします。 |
| `InContainerMountStrategy(pattern=FuseMountPattern(...))` | イメージに `blobfuse2` と FUSE サポートがある場合。 | `AzureBlobMount` をサポートします。 |
| `InContainerMountStrategy(pattern=S3FilesMountPattern(...))` | イメージに `mount.s3files` があり、既存の S3 Files mount ターゲットに到達できる場合。 | `S3FilesMount` をサポートします。 |
| `DockerVolumeMountStrategy(driver=...)` | コンテナ起動前に Docker が volume-driver ベースの mount を接続すべき場合。 | Docker 専用です。 S3 、 GCS 、 R2 、 Azure Blob 、 Box は `rclone` をサポートし、 S3 と GCS は `mountpoint` もサポートします。 |

</div>

## 対応するホスト型プラットフォーム

ホスト型環境が必要な場合でも、通常は同じ `SandboxAgent` 定義をそのまま使え、 [`SandboxRunConfig`][agents.run_config.SandboxRunConfig] で sandbox クライアントのみを変更します。

このリポジトリのチェックアウト版ではなく公開済み SDK を使っている場合は、対応するパッケージ extra を通じて sandbox-client 依存関係をインストールしてください。

プロバイダー固有のセットアップに関する注意点や、リポジトリに含まれる拡張の例へのリンクについては、 [examples/sandbox/extensions/README.md](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/README.md) を参照してください。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| クライアント | インストール | 例 |
| --- | --- | --- |
| `BlaxelSandboxClient` | `openai-agents[blaxel]` | [Blaxel runner](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/blaxel_runner.py) |
| `CloudflareSandboxClient` | `openai-agents[cloudflare]` | [Cloudflare runner](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/cloudflare_runner.py) |
| `DaytonaSandboxClient` | `openai-agents[daytona]` | [Daytona runner](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/daytona/daytona_runner.py) |
| `E2BSandboxClient` | `openai-agents[e2b]` | [E2B runner](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/e2b_runner.py) |
| `ModalSandboxClient` | `openai-agents[modal]` | [Modal runner](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/modal_runner.py) |
| `RunloopSandboxClient` | `openai-agents[runloop]` | [Runloop runner](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/runloop/runner.py) |
| `VercelSandboxClient` | `openai-agents[vercel]` | [Vercel runner](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/vercel_runner.py) |

</div>

ホスト型 sandbox クライアントは、プロバイダー固有の mount 戦略を公開しています。ストレージプロバイダーに最も適したバックエンドと mount 戦略を選択してください。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| バックエンド | mount に関する注記 |
| --- | --- |
| Docker | `S3Mount` 、 `GCSMount` 、 `R2Mount` 、 `AzureBlobMount` 、 `BoxMount` 、 `S3FilesMount` を、 `InContainerMountStrategy` や `DockerVolumeMountStrategy` などのローカル戦略でサポートします。 |
| `ModalSandboxClient` | `S3Mount` 、 `R2Mount` 、 HMAC 認証された `GCSMount` に対して、 `ModalCloudBucketMountStrategy` による Modal cloud bucket mount をサポートします。インライン認証情報または名前付き Modal Secret を使用できます。 |
| `CloudflareSandboxClient` | `S3Mount` 、 `R2Mount` 、 HMAC 認証された `GCSMount` に対して、 `CloudflareBucketMountStrategy` による Cloudflare bucket mount をサポートします。 |
| `BlaxelSandboxClient` | `S3Mount` 、 `R2Mount` 、 `GCSMount` に対して、 `BlaxelCloudBucketMountStrategy` による cloud bucket mount をサポートします。また、 `agents.extensions.sandbox.blaxel` の `BlaxelDriveMount` と `BlaxelDriveMountStrategy` による永続的な Blaxel Drive もサポートします。 |
| `DaytonaSandboxClient` | `DaytonaCloudBucketMountStrategy` による rclone ベースの cloud storage mount をサポートします。`S3Mount` 、 `GCSMount` 、 `R2Mount` 、 `AzureBlobMount` 、 `BoxMount` と組み合わせて使用します。 |
| `E2BSandboxClient` | `E2BCloudBucketMountStrategy` による rclone ベースの cloud storage mount をサポートします。`S3Mount` 、 `GCSMount` 、 `R2Mount` 、 `AzureBlobMount` 、 `BoxMount` と組み合わせて使用します。 |
| `RunloopSandboxClient` | `RunloopCloudBucketMountStrategy` による rclone ベースの cloud storage mount をサポートします。`S3Mount` 、 `GCSMount` 、 `R2Mount` 、 `AzureBlobMount` 、 `BoxMount` と組み合わせて使用します。 |
| `VercelSandboxClient` | 現時点ではホスト型固有の mount 戦略は公開されていません。代わりに manifest ファイル、リポジトリ、またはその他のワークスペース入力を使用してください。 |

</div>

以下の表は、各バックエンドがどのリモートストレージエントリを直接マウントできるかをまとめたものです。

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

さらに実行可能な例については、ローカル、コーディング、メモリ、ハンドオフ、エージェント構成パターンは [examples/sandbox/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox) を、ホスト型 sandbox クライアントについては [examples/sandbox/extensions/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox/extensions) を参照してください。