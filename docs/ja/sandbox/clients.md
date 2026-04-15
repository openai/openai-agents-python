---
search:
  exclude: true
---
# Sandbox クライアント

このページでは、sandbox 作業をどこで実行するかを選択します。ほとんどの場合、`SandboxAgent` の定義は同じままで、sandbox クライアントとクライアント固有オプションのみが [`SandboxRunConfig`][agents.run_config.SandboxRunConfig] で変わります。

!!! warning "Beta 機能"

    Sandbox エージェントは beta です。一般提供までに API の詳細、デフォルト、サポートされる機能は変更される可能性があり、時間とともにより高度な機能が追加される想定です。

## 判断ガイド

<div class="sandbox-nowrap-first-column-table" markdown="1">

| 目的 | まず使うもの | 理由 |
| --- | --- | --- |
| macOS または Linux で最速のローカル反復 | `UnixLocalSandboxClient` | 追加インストール不要で、ローカルファイルシステム開発がシンプルです。 |
| 基本的なコンテナ分離 | `DockerSandboxClient` | 特定のイメージを使って Docker 内で作業を実行します。 |
| ホスト型実行または本番相当の分離 | ホスト型 sandbox クライアント | ワークスペース境界をプロバイダー管理環境へ移します。 |

</div>

## ローカルクライアント

ほとんどのユーザーは、まず次の 2 つの sandbox クライアントのいずれかから始めてください。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| クライアント | インストール | 選ぶタイミング | 例 |
| --- | --- | --- | --- |
| `UnixLocalSandboxClient` | なし | macOS または Linux で最速のローカル反復をしたい場合。ローカル開発の良いデフォルトです。 | [Unix-local スターター](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/unix_local_runner.py) |
| `DockerSandboxClient` | `openai-agents[docker]` | コンテナ分離や、ローカル一致性のために特定イメージが必要な場合。 | [Docker スターター](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) |

</div>

Unix-local は、ローカルファイルシステムを対象に開発を始める最も簡単な方法です。より強い環境分離や本番相当の一致性が必要になったら、Docker またはホスト型プロバイダーに移行してください。

Unix-local から Docker に切り替えるには、エージェント定義は同じままにして run config のみ変更します。

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

これはコンテナ分離やイメージ一致性が必要な場合に使用します。[examples/sandbox/docker/docker_runner.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docker/docker_runner.py) を参照してください。

## マウントとリモートストレージ

mount エントリは公開するストレージを記述し、mount strategy は sandbox バックエンドがそのストレージをどのように接続するかを記述します。組み込みの mount エントリと汎用 strategy は `agents.sandbox.entries` から import します。ホスト型プロバイダー向け strategy は `agents.extensions.sandbox` またはプロバイダー固有の拡張パッケージから利用できます。

一般的な mount オプション:

- `mount_path`: sandbox 内でストレージが現れる場所です。相対パスは manifest ルート配下で解決され、絶対パスはそのまま使われます。
- `read_only`: デフォルトは `True` です。sandbox がマウント先ストレージへ書き戻す必要がある場合のみ `False` にします。
- `mount_strategy`: 必須です。mount エントリと sandbox バックエンドの両方に一致する strategy を使ってください。

mount は一時的なワークスペースエントリとして扱われます。スナップショットと永続化のフローでは、マウントされたリモートストレージを保存済みワークスペースへコピーする代わりに、マウントパスを切り離すかスキップします。

汎用のローカル/コンテナ strategy:

<div class="sandbox-nowrap-first-column-table" markdown="1">

| strategy またはパターン | 使うタイミング | 注記 |
| --- | --- | --- |
| `InContainerMountStrategy(pattern=RcloneMountPattern(...))` | sandbox イメージで `rclone` を実行できる場合。 | S3、GCS、R2、Azure Blob をサポートします。`RcloneMountPattern` は `fuse` mode または `nfs` mode で実行できます。 |
| `InContainerMountStrategy(pattern=MountpointMountPattern(...))` | イメージに `mount-s3` があり、Mountpoint スタイルの S3 または S3 互換アクセスが必要な場合。 | `S3Mount` と `GCSMount` をサポートします。 |
| `InContainerMountStrategy(pattern=FuseMountPattern(...))` | イメージに `blobfuse2` と FUSE サポートがある場合。 | `AzureBlobMount` をサポートします。 |
| `InContainerMountStrategy(pattern=S3FilesMountPattern(...))` | イメージに `mount.s3files` があり、既存の S3 Files マウント先に到達できる場合。 | `S3FilesMount` をサポートします。 |
| `DockerVolumeMountStrategy(driver=...)` | コンテナ開始前に Docker で volume driver ベースのマウントを接続する必要がある場合。 | Docker 専用です。S3、GCS、R2、Azure Blob は `rclone` をサポートし、S3 と GCS は `mountpoint` もサポートします。 |

</div>

## 対応ホスト型プラットフォーム

ホスト型環境が必要な場合、通常は同じ `SandboxAgent` 定義をそのまま使え、[`SandboxRunConfig`][agents.run_config.SandboxRunConfig] で sandbox クライアントだけを変更します。

このリポジトリの checkout ではなく公開済み SDK を使用している場合は、対応する package extra で sandbox-client 依存関係をインストールしてください。

プロバイダー固有のセットアップメモと、リポジトリ内 extension examples へのリンクは [examples/sandbox/extensions/README.md](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/extensions/README.md) を参照してください。

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

ホスト型 sandbox クライアントはプロバイダー固有の mount strategy を公開しています。ストレージプロバイダーに最適なバックエンドと mount strategy を選択してください。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| バックエンド | mount に関する注意 |
| --- | --- |
| Docker | `InContainerMountStrategy` や `DockerVolumeMountStrategy` などのローカル strategy で、`S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount`、`S3FilesMount` をサポートします。 |
| `ModalSandboxClient` | `S3Mount`、`R2Mount`、HMAC 認証 `GCSMount` に対して `ModalCloudBucketMountStrategy` で Modal cloud bucket mount をサポートします。インライン認証情報または名前付き Modal Secret を使用できます。 |
| `CloudflareSandboxClient` | `S3Mount`、`R2Mount`、HMAC 認証 `GCSMount` に対して `CloudflareBucketMountStrategy` で Cloudflare bucket mount をサポートします。 |
| `BlaxelSandboxClient` | `S3Mount`、`R2Mount`、`GCSMount` に対して `BlaxelCloudBucketMountStrategy` で cloud bucket mount をサポートします。さらに `agents.extensions.sandbox.blaxel` の `BlaxelDriveMount` と `BlaxelDriveMountStrategy` で永続的な Blaxel Drives もサポートします。 |
| `DaytonaSandboxClient` | `DaytonaCloudBucketMountStrategy` で cloud bucket mount をサポートします。`S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount` と組み合わせて使用します。 |
| `E2BSandboxClient` | `E2BCloudBucketMountStrategy` で cloud bucket mount をサポートします。`S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount` と組み合わせて使用します。 |
| `RunloopSandboxClient` | `RunloopCloudBucketMountStrategy` で cloud bucket mount をサポートします。`S3Mount`、`GCSMount`、`R2Mount`、`AzureBlobMount` と組み合わせて使用します。 |
| `VercelSandboxClient` | 現時点ではホスト型固有の mount strategy は公開されていません。代わりに manifest ファイル、repo、または他のワークスペース入力を使用してください。 |

</div>

下の表は、各バックエンドがどのリモートストレージエントリを直接マウントできるかを要約したものです。

<div class="sandbox-nowrap-first-column-table" markdown="1">

| バックエンド | AWS S3 | Cloudflare R2 | GCS | Azure Blob Storage | S3 Files |
| --- | --- | --- | --- | --- | --- |
| Docker | ✓ | ✓ | ✓ | ✓ | ✓ |
| `ModalSandboxClient` | ✓ | ✓ | ✓ | - | - |
| `CloudflareSandboxClient` | ✓ | ✓ | ✓ | - | - |
| `BlaxelSandboxClient` | ✓ | ✓ | ✓ | - | - |
| `DaytonaSandboxClient` | ✓ | ✓ | ✓ | ✓ | - |
| `E2BSandboxClient` | ✓ | ✓ | ✓ | ✓ | - |
| `RunloopSandboxClient` | ✓ | ✓ | ✓ | ✓ | - |
| `VercelSandboxClient` | - | - | - | - | - |

</div>

実行可能な例をさらに確認するには、ローカル、コーディング、メモリ、ハンドオフ、エージェント構成パターンについては [examples/sandbox/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox) を、ホスト型 sandbox クライアントについては [examples/sandbox/extensions/](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox/extensions) を参照してください。