# Minecraft観測テスト

## 一括実行

`individual_launchers/start_minecraft_environment.bat`をダブルクリックし、起動完了を待ってから`tests/minecraft_observation/run_tests_docker.bat`をダブルクリックします。BATは `tests/minecraft_observation/` のテストをまとめて実行します。変更・削除してもよいワールドを使用してください。`start_minecraft_environment.bat`で起動するワールドを書き換えたくない場合は、Minecraft・Mineflayerを停止し、`minecraft_server_on_docker/flat/25565/data`フォルダを退避してから起動してください。起動時に新しいワールドが作成されます。退避したデータを戻す場合も、両サービスを停止してから行ってください。テスト自身はサーバーを起動・停止しません。

一括BATは、観測・可視性・履歴・プロンプトの正しさの確認、固定データによる性能測定、実Minecraftでの観測性能測定をすべて実行します。実機測定は30秒の区間が計13回あり、測定だけで6分30秒かかります。24時間相当の模擬履歴を作るテストだけは、個別に指定した場合のみ実行されます。個別実行については後述します。

Docker Desktopを起動し、25565番ポートのMinecraftサーバーとMineflayerの起動完了を待ってください。BATは事前準備を表示してEnter入力を待ちます。準備が完了したことは利用者が確認してください。終了後は結果画面がキー入力を待ちます。

## テストファイル

- `test_01_observation_processing.py`: 受信順序、複数tickの処理、処理完了通知
- `test_02_visibility.py`: 空間索引、可視性の差分、視距離の正しさ
- `test_03_observation_history.py`: 過去状態の復元、履歴の不変性、履歴長ごとの性能と模擬的な1日分の容量
- `test_04_observation_prompt.py`: プロンプトの内容、受信済み観測との同期、固定データによる生成時間
- `test_05_observation_in_minecraft.py`: 実Minecraftでの観測処理、チャット、EnvBox・視距離比較
- `diagnostic_multiprocess.py`: 10体を各1 Pythonプロセスで動かす観測性能診断（個別実行のみ）

## 個別実行（必要な場合のみ）

一部のテストだけを実行する場合は、BATではなく以下のコマンドを使います。

PowerShellでは、リポジトリのルートで次のように実行します。`-p` はファイル、`-k` はテスト関数名の一部で絞り込みます。実Minecraftが不要なテストに対してはMinecraftとMineflayerを起動する必要はありません。

```powershell
$testMount = (Resolve-Path tests).Path + ':/app/tests:ro'
docker compose run --build --rm --no-deps -w /app -v $testMount -e PYTHONPATH=/app/src python_runtime python -m unittest discover -s tests/minecraft_observation -p test_02_visibility.py -v
```

通常の `unittest discover` でも性能測定と実Minecraftテストが実行されます。全件を実行する場合は変更・削除してもよいワールドでMinecraftとMineflayerを起動し、コンテナから接続するために `EASYLLM_TEST_MINEFLAYER_HOST=mineflayer` と `EASYLLM_TEST_OBSERVATION_HOST=minecraft` を指定します。BATはこれらを設定します。

10体を別プロセスで測る場合は、変更・削除してもよいワールドを起動して次を実行します。静止と歩行を各20秒測り、受信処理時間、処理済みtickの速度、観測キューとMemoryの遅れを表示します。

```powershell
$testMount = (Resolve-Path tests).Path + ':/app/tests:ro'
docker compose --profile manual run --rm --no-deps -w /app -v $testMount -e PYTHONPATH=/app/src -e EASYLLM_TEST_MINEFLAYER_HOST=mineflayer -e EASYLLM_TEST_OBSERVATION_HOST=minecraft python_runtime python /app/tests/minecraft_observation/diagnostic_multiprocess.py
```

一括の実機測定では、通常ケースは1体または3体・10体の静止と歩行、チャット反映、見えるブロックの更新を扱います。EnvBoxと視距離の比較も行います。LLMとの通信や音声認識は行いません。`EASYLLM_TEST_OBSERVATION_DURATION`、`EASYLLM_TEST_CHAT_TRIALS`、`EASYLLM_TEST_ENV_BOX`、`EASYLLM_TEST_ENV_BOXES`、`EASYLLM_TEST_VISIBILITY_DISTANCES` などで条件を変えられます。処理時間、観測待ち行列、処理済みtickの速度をJSONで表示します。

## 補足：入力待ちの省略

コマンドプロンプトを使う場合は、BATのあるフォルダで`run_tests_docker.bat --yes`を実行すると入力待ちを省略できます。
