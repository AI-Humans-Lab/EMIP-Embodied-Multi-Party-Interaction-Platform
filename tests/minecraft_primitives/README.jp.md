# Minecraft上のプリミティブ動作テスト

テストには次の2種類の実行方法があります。MinecraftとMineflayerはいずれもDockerで起動します。

- **DockerのPythonで実行**: BATをダブルクリックし、一時コンテナでテストを実行します。ホストのPythonは不要です。
- **ホストのPythonで実行**: Pythonをローカルに用意し、コマンドで全件・特定のテストを選んで実行します。

ホスト実行時はMineflayerの`127.0.0.1:3000`とMinecraft観測用の`127.0.0.1:7891`に接続し、Docker実行時はCompose内のサービス名`mineflayer`と`minecraft`に接続します。Minecraftクライアントの接続先は`localhost:25565`です。

対象は`craftItem`、`goToPosition`、`mineBlock`、`placeItem`、`smeltItem`、チェスト操作、レバー操作、`giveItemToOther`、`receiveItemFromOther`、`think`です。アイテム転送のテストでは2体目のMineflayer botを使用します。

**変更・削除してもよいワールドを使用してください。**各シナリオはテスト領域のブロック、エージェントの位置・インベントリを初期化し、ワールド内の地面のアイテムをすべて削除します。Docker Composeは常に`minecraft_server_on_docker/flat/25565/data`を使用します。このワールドを書き換えたくない場合は、Minecraft・Mineflayerを停止し、`data`フォルダを退避してから起動してください。起動時に新しいワールドが作成されます。退避したデータを戻す場合も、両サービスを停止してから行ってください。スクリプトではデータを入れ替えません。

## テストの実行

Docker Desktopを起動し、必要に応じてデータを退避してから、`individual_launchers/start_minecraft_environment.bat`をダブルクリックしてMinecraftとMineflayerを起動します。Agentは起動しません。Minecraftサーバーは`docker-compose.yml`のFabric 1.21とeasy-llm Mod、Mineflayerサーバーは同じCompose設定を使います。

### DockerのPython

MinecraftとMineflayerの起動後、`tests/minecraft_primitives/run_tests_docker.bat`をダブルクリックします。既定ではMinecraft内でテスト名と結果を確認でき、最初のシナリオまで30秒、シナリオ間も一時停止します。テスト終了後は結果画面がキー入力を待ちます。

BATは事前準備を表示してEnter入力を待ちます。準備が完了したことは利用者が確認してください。

### ホストのPython（必要な場合のみ）

ホストで実行する場合だけ、Python 3.11と必要なパッケージをインストールします。以下のPowerShellコマンドは、すべてリポジトリのルート（`docker-compose.yml`があるフォルダ）で実行してください。

```powershell
python -m pip install "numpy>=1.26" "websockets>=12" websocket-client pyyaml
```

MinecraftとMineflayerの起動後、別のPowerShellターミナルでテストの読み込み先と実行フラグを設定します。

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
$env:EASYLLM_RUN_MINECRAFT_PRIMITIVES = "1"
```

全件実行:

```powershell
python -m unittest discover -s tests/minecraft_primitives -v
```

特定のプリミティブだけ実行（例: `craftItem`）:

```powershell
python -m unittest discover -s tests/minecraft_primitives -v -k test_craftItem_
```

1件だけ実行（例: `goToPosition`の07番）:

```powershell
python -m unittest discover -s tests/minecraft_primitives -v -k test_goToPosition_07_
```

`-k`はテスト名の一部で絞り込みます。一致するテストが複数あれば、そのすべてを実行します。名前を探すには`Select-String -Path tests/minecraft_primitives/test_primitives_in_minecraft.py -Pattern 'async def test_'`を使えます。テスト名や結果をMinecraft内で見ながら実行する場合は、実行前に`$env:EASYLLM_WATCH_MINECRAFT_PRIMITIVES = "1"`も設定してください。最初のシナリオまで30秒待機し、シナリオ間でも一時停止します。

通常のテスト検出では、上記の実行フラグか観察モードを有効にしない限り、この実Minecraftテストはスキップされます。ホスト側で設定した環境変数は`Remove-Item Env:\EASYLLM_RUN_MINECRAFT_PRIMITIVES, Env:\EASYLLM_WATCH_MINECRAFT_PRIMITIVES -ErrorAction SilentlyContinue`で解除できます。

どちらの方法もMinecraft・Mineflayerの停止やワールドデータの入れ替えは行いません。データを退避した場合は、終了後に両コンテナを停止してから元に戻してください。Pythonの観測ログは一時フォルダの`easyllm-minecraft-primitives`に保存されます（Docker実行時は一時コンテナとともに消えます）。

## 補足：入力待ちの省略

コマンドプロンプトを使う場合は、BATのあるフォルダで`run_tests_docker.bat --yes`を実行すると入力待ちを省略できます。観察用の待機も省略する場合は`run_tests_docker.bat -Batch --yes`を使います。
