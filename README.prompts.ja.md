# EMIP — プロンプトの変更方法

**このREADMEは、EMIPを起動できるようになり、エージェントの話し方や行動方針、LLMへ渡す情報を変更してみたい人向けの手順書です。**

初回の環境構築と音声接続は[起動用README](./README.ja.md)を参照してください。ここでは、Sallyの設定を例に、プロンプトをコピーして編集し、読み込み先を変更して試す方法を説明します。

| やりたいこと | 読む場所 |
| --- | --- |
| 使用中のモードと編集対象を確認する | [1. 使用中のモードを確認する](#1-使用中のモードを確認する)、[2. 編集するファイルを選ぶ](#2-編集するファイルを選ぶ) |
| 通常の行動生成で、発言や行動の方針を変える | [3. 通常の行動生成のプロンプトを変更する](#3-通常の行動生成のプロンプトを変更する) |
| GPT-Liveの会話の仕方を変える | [4. GPT-Liveの会話プロンプトを変更する](#4-gpt-liveの会話プロンプトを変更する) |
| 自動判定で、行動を始める条件を変える | [5. 自動判定の質問を変更する](#5-自動判定の質問を変更する) |
| 観測情報の内容や並びを変える | [6. テンプレートを編集する](#6-テンプレートを編集する) |
| 変更を反映し、結果を確認する | [7. 再起動して変更を確認する](#7-再起動して変更を確認する) |

## 1. 使用中のモードを確認する

プロンプトによって、使われるモードが異なります。編集前に、エクスプローラーで**EMIPフォルダ → src → runtime_settings.py**を開き、`VOICE_MODE`と`DECISION_MODE`を確認してください。EMIPフォルダは、`docker-compose.yml`がある場所です。

### 音声モード

| `runtime_settings.py`の指定 | モード | 主に使うプロンプト |
| --- | --- | --- |
| `VOICE_MODE = VOICE_MODE_LIST[0]` | `none`：音声なし | 行動生成用の`generate_action`と`primitive` |
| `VOICE_MODE = VOICE_MODE_LIST[1]` | `asr_tts`：音声認識と音声合成 | 行動生成用の`generate_action`と`primitive`。生成したチャット発言を音声合成します。 |
| `VOICE_MODE = VOICE_MODE_LIST[2]` | `gpt_live`：直接音声対話 | 会話用の`gpt_live`。ゲーム内行動を依頼すると、行動生成用プロンプトと`delegated_actions`も使います。 |

### 行動開始モード

| `runtime_settings.py`の指定 | 動作 |
| --- | --- |
| `DECISION_MODE = DECISION_MODE_LIST[0]` | `enter`。`asr_tts`と`none`では、SallyのウィンドウでEnterを押すと行動を生成します。`gpt_live`では、音声対話側からの行動依頼を受けて実行します。 |
| `DECISION_MODE = DECISION_MODE_LIST[1]` | `decision_api`。行動生成が必要かをAPIで判定します。`asr_tts`または`none`と組み合わせます。 |

GPT-Liveを使う場合は、`DECISION_MODE`を`enter`にします。この組み合わせで会話や行動を進める際に、通常のEnter入力は使いません。

起動用READMEは`asr_tts`と`enter`の組み合わせを説明しています。設定を変更して使っている場合は、**現在のファイルに書かれたモード**を基準に、以下の編集例を選んでください。

## 2. 編集するファイルを選ぶ

Sallyのプロンプトの参照先は[src/sally_config.py](./src/sally_config.py)の`AGENT_CFG.prompts`にまとまっています。`human`はLLMへ渡す入力側の分類名であり、人間の利用者が直接入力する文章だけを指すものではありません。

### 用途と参照先

| 変更したい内容 | 設定のキー | 同梱の参照先（`src/`からの相対パス） |
| --- | --- | --- |
| 観測情報の並び、通常の行動生成への依頼、発言・行動の方針 | `prompts["human"]["generate_action"]` | [prompts/coding_llm_human_prompt_template_jp.txt](./src/prompts/coding_llm_human_prompt_template_jp.txt) |
| 使用できる操作関数、行動コードの生成規則、出力形式 | `prompts["system"]["primitive"]` | [prompts/coding_llm_system_prompt_template_jp.txt](./src/prompts/coding_llm_system_prompt_template_jp.txt) |
| GPT-Liveの話し方、ゲーム内行動を依頼する判断 | `prompts["system"]["gpt_live"]` | [prompts/gpt_live_instructions.txt](./src/prompts/gpt_live_instructions.txt) |
| GPT-Liveから行動生成へ渡す会話履歴・実行結果の説明 | `prompts["human"]["delegated_actions"]` | [prompts/delegated_actions_prompt.txt](./src/prompts/delegated_actions_prompt.txt) |
| 自動判定で「今、行動生成が必要か」を判断する質問 | `prompts["human"]["decision"]` | [prompts/decision_prompt_template_jp.txt](./src/prompts/decision_prompt_template_jp.txt) |
| 他者Beliefを入力へ含める際の表示内容 | `prompts["other_belief"]` | 同梱設定ではファイルではなく、`{{ events() }}`を直接記載 |

`src/prompts/`には、`_jp`の付かない行動生成用テンプレートもあります。**実際に使用するのは、エージェント設定から参照されているファイルです。** ファイル名やMinecraftの表示言語によって、自動的に切り替わる仕組みではありません。

SallyとAnneは複数のプロンプトファイルを共有しています。共有ファイルを直接編集すると、次回起動時に両方へ反映されます。Sallyだけを変える場合は、以下のようにファイルをコピーして、Sallyの参照先だけを変更します。

> [!NOTE]
> プロンプトで変更できるのは、LLMへの指示や入力の構成です。新しい操作関数、観測できる情報、実行制御そのものを追加する場合は、対応するプログラムの変更も必要です。

## 3. 通常の行動生成のプロンプトを変更する

ここでは、**`asr_tts`または`none`で、Sallyの発言を短くし、不明な依頼を確認する方針を加える**例を示します。GPT-Liveの音声での話し方を変えたい場合は、[4](#4-gpt-liveの会話プロンプトを変更する)を使ってください。

### 3.1 元のファイルをコピーする

1. エクスプローラーで**EMIPフォルダ → src → prompts**を開きます。
2. `coding_llm_human_prompt_template_jp.txt`をコピーします。
3. コピーしたファイルを`coding_llm_human_prompt_custom_jp.txt`という名前にします。
4. コピーしたファイルをメモ帳などのテキストエディターで開きます。

Windowsで拡張子が非表示の場合は、エクスプローラーの**表示 → 表示 → ファイル名拡張子**を有効にします。`.txt.txt`にならないよう確認してください。

### 3.2 方針を追記する

コピーしたファイルの末尾へ、例えば次を追記します。既存の観測情報や出力形式に関する指示は残します。

```text
## 発言と協力の方針
- 発言は、原則として短い1～2文にまとめてください。
- 依頼された対象や場所が分からない場合は、作業を始める前に短く確認してください。
- 観測から確認できないことは、分からないと伝えてください。
- 実行結果を確認する前に、作業が完了したとは言わないでください。
- 発言も、既存の出力形式に従ってbot.chat(...)の行動コードとして生成してください。
```

`Ctrl + S`で、文字コードを**UTF-8**にして保存します。この例は既存の指示への追記です。ファイル全体をこの文章だけに置き換えないでください。

### 3.3 Sallyの参照先を変更する

`src/sally_config.py`をテキストエディターで開き、`prompts`の`human`内にある次の行を探します。

変更前：

```text
"generate_action": "./prompts/coding_llm_human_prompt_template_jp.txt",
```

変更後：

```text
"generate_action": "./prompts/coding_llm_human_prompt_custom_jp.txt",
```

この1行のパスだけを変更し、引用符・カンマ・周囲の括弧は残します。`Ctrl + S`で保存してください。

パスは、同梱BATで起動する場合は`src/`からの相対パスです。PC上の保存場所が`EMIPフォルダ/src/prompts/coding_llm_human_prompt_custom_jp.txt`なら、設定値は`./prompts/coding_llm_human_prompt_custom_jp.txt`になります。

**ここまでの確認：コピーしたファイルに方針が追記され、Sallyの`generate_action`がそのコピーを参照している。** 次に[7. 再起動して変更を確認する](#7-再起動して変更を確認する)へ進みます。

## 4. GPT-Liveの会話プロンプトを変更する

GPT-Liveで話す内容や口調は、`gpt_live_instructions.txt`で設定します。ゲーム内行動は別の行動生成処理へ依頼するため、会話の指示と行動生成の指示を分けて考えます。

### 4.1 会話用ファイルをコピーして編集する

1. `src/prompts/gpt_live_instructions.txt`をコピーします。
2. コピーを`gpt_live_instructions_custom.txt`という名前にします。
3. コピーしたファイルの末尾へ、例えば次を追記します。

```text
## 会話の方針
- 発言は、原則として短い1～2文にまとめてください。
- 操作方法を聞かれた場合は、最初に行う操作を一つずつ説明してください。
- 質問や依頼への返答を優先し、求められていない長い状況説明は控えてください。
- 聞き取れなかった場合や依頼が曖昧な場合は、短く確認してください。
```

元からある`Delegation policy`以降の行動依頼に関する指示と、「実行結果が確認されるまで、行動が成功したとは言わない」という指示は残します。これらは、音声での返答とゲーム内行動を結び付けるための記述です。

先頭付近の`{{ agent_name }}`も、そのまま残してください。起動時に`sally`などのエージェント名へ置き換わります。このファイルでは、この表記を文字列として置き換えるため、**括弧内の空白も含めて`{{ agent_name }}`を維持**します。

### 4.2 参照先を切り替える

`src/sally_config.py`の`prompts`の`system`内を、次のように変更します。

変更前：

```text
"gpt_live": "./prompts/gpt_live_instructions.txt",
```

変更後：

```text
"gpt_live": "./prompts/gpt_live_instructions_custom.txt",
```

保存後、[7](#7-再起動して変更を確認する)の手順でSallyを再起動します。`VOICE_MODE`が`gpt_live`であることを確認してください。`asr_tts`のままでは、この会話用プロンプトは使われません。

### 4.3 行動依頼に渡す情報を変える場合

`delegated_actions_prompt.txt`は、GPT-Liveから行動生成へ渡す追加情報のテンプレートです。会話履歴と確認された行動実行結果を、次の表記で埋め込みます。

| 表記 | 埋め込まれる情報 |
| --- | --- |
| `{{ conversation_history }}` | ユーザーとGPT-Liveの発言・文字起こしの履歴 |
| `{{ action_results }}` | 確認された行動実行結果 |

このテンプレートを変更する場合も、ファイルをコピーし、`prompts["human"]["delegated_actions"]`の参照先を切り替えます。両方の表記を残したまま、周囲の説明を調整してください。

GPT-Liveから依頼されたゲーム内行動の生成には、`generate_action`と`primitive`も使われます。移動や採掘の方針を変える場合は、会話用ファイルに加えて、これらの内容も確認します。

## 5. 自動判定の質問を変更する

`decision_prompt_template_jp.txt`は、`decision_api`モードで**今、行動生成を始める必要があるか**を判断する質問です。次に実行するJavaScriptを生成するためのプロンプトとは役割が異なります。

1. `src/prompts/decision_prompt_template_jp.txt`をコピーし、`decision_prompt_custom_jp.txt`とします。
2. コピーしたファイルで、行動生成を必要とする条件を調整します。
3. `src/sally_config.py`の`human`内の参照先を変更します。

```text
"decision": "./prompts/decision_prompt_custom_jp.txt",
```

例えば、挨拶にも反応させたい場合は、同梱の「挨拶・感想だけの場合は該当しない」という条件を、次のように置き換えます。

```text
自分に向けられた未対応の挨拶や質問、未完了の依頼がある場合は、行動生成が必要です。
すでに返答済みの挨拶や完了した依頼には、繰り返し反応しないでください。
```

反対の指示を追記したまま元の条件を残すと、判断の基準が曖昧になります。変更する条件を置き換え、観測内の命令文を判定用の指示として扱わないという既存の注意は残してください。

この変更を試す場合は、`src/runtime_settings.py`で`DECISION_MODE`を`DECISION_MODE_LIST[1]`にします。音声モードは`asr_tts`または`none`を使います。判定の周期と閾値も、同じファイルの`DECISION_INTERVAL_SECONDS`と`DECISION_THRESHOLD`で指定します。

`enter`やGPT-Liveで動かしている場合、この判定用ファイルを編集しても判定は行われません。Anneにも同じ判定を使わせる場合は、`anne_config.py`の`human`内へ`decision`の参照を追加する必要があります。同梱のAnne設定には、このキーがありません。

## 6. テンプレートを編集する

話し方の変更だけであれば、[3](#3-通常の行動生成のプロンプトを変更する)または[4](#4-gpt-liveの会話プロンプトを変更する)の追記で試せます。この節では、LLMへ渡す情報や出力規則も調整したい場合の書き方を説明します。

### 6.1 観測情報を埋め込む表記

行動生成用の`generate_action`は、Jinja2というテンプレート形式を使っています。通常の文章はそのまま渡され、`{{ ... }}`の部分は実行時の情報へ置き換わります。`{% ... %}`は繰り返しなどを表します。

同梱テンプレートには、次の表記があります。

| 表記 | 内容 |
| --- | --- |
| `{{ self_name }}` | エージェント自身の名前 |
| `{{ thought() }}` | Belief内に記録された思考情報 |
| `{{ chat_log() }}` | チャットの履歴 |
| `{{ position(self_name) }}` | 自分の位置 |
| `{{ inventory(self_name) }}` | 自分の所持品 |
| `{{ chests() }}` | Belief内のチェスト情報 |
| `{{ other_players(self_name) }}` | 自分以外のプレイヤー情報 |
| `{{ blocks_and_visibilities(["oak_log", "stone"]) }}` | 指定した種類のブロックと可視性の情報 |
| `{{ events_and_visibilities() }}` | 出来事と可視性の履歴 |
| `{% for person, text in other_beliefs.items() %}`から`{% endfor %}` | エージェントが推定した他者のBeliefを順番に表示する部分 |

Beliefは、エージェントが観測などを通じて保持している情報です。他者のBeliefの表示は、その人物の実際の考えを直接取得するものではありません。

例えば、LLMへ渡すブロック情報を原木・石・鉄鉱石に絞る場合は、コピーした行動生成用テンプレートの`blocks_and_visibilities(...)`の行を、次に置き換えます。

```text
{{ blocks_and_visibilities(["oak_log", "stone", "iron_ore"]) }}
```

このリストは、保持している情報から何をプロンプトに表示するかを指定します。観測範囲や取得できる情報そのものは広がりません。

括弧や引用符、`{% endfor %}`などの対応を保ってください。定義されていない変数名や関数名を追加すると、テンプレートの展開時にエラーになります。

### 6.2 ファイルによる表記の違い

| 編集するプロンプト | 使える表記の扱い |
| --- | --- |
| `generate_action` | 上の表の観測関数や`self_name`などを展開します。 |
| `delegated_actions` | `{{ conversation_history }}`と`{{ action_results }}`を展開します。 |
| `gpt_live` | `{{ agent_name }}`を名前へ置き換えます。他のテンプレートの変数や観測関数は展開しません。 |
| `primitive`、`decision` | 記載した文章をそのまま使います。`{{ ... }}`を追加しても観測情報へ置き換わりません。 |

`other_belief`は、他者のBeliefをどの文章で表示するかを指定します。同梱の`{{ events() }}`は、そのBelief内の出来事を表示する設定です。他者Beliefを使うかどうかは、エージェント設定の`TRACK_OTHER_BELIEFS`も確認してください。自動判定の入力では、他者Beliefの一覧は空として扱われます。

### 6.3 GPT-Liveでも使われる見出し

`generate_action`は、通常の行動生成に加え、GPT-Liveへの観測通知と自動判定の入力にも使われます。情報を削ったり並べ替えたりすると、これらの入力も変わります。

特にGPT-Liveでは、次の見出しをプログラムが読み取ります。

| 見出し | GPT-Liveでの扱い |
| --- | --- |
| `# あなたのタスク` | この見出しより前の部分を、観測通知の対象にします。見出しと、それより後の行動生成への指示は通知から除きます。 |
| `## 目的`、`## 行動方針` | これらの節がある場合、観測通知から分離し、起動時のGPT-Liveの指示へ加えます。 |

見出しを変更すると、この振り分けも変わります。既存の`# あなたのタスク`は表記と位置を維持してください。`## 目的`と`## 行動方針`は、この共有を意図する場合に使います。これらの節は次の見出しまでで区切られ、GPT-Liveの初期指示へ加える際には`self_name`と`other_name`だけが渡されます。観測関数は、観測情報を表示する部分に記載してください。

GPT-Liveへの観測通知には、`runtime_settings.py`の`GPT_LIVE_SETTINGS.observation_character_limit`による文字数上限があります。長いテンプレートでは後半が通知に含まれない場合があります。

### 6.4 行動コードの出力形式を維持する

`primitive`を変更する場合も、元のファイルをコピーし、`sally_config.py`の`prompts["system"]["primitive"]`をそのコピーへ切り替えます。

このファイルには、使用できる操作関数と出力規則が書かれています。現在の行動実行処理は、次のような**`code`配列を持つJSON**を受け取ります。

```json
{
  "code": [
    "bot.chat('こんにちは！')"
  ]
}
```

`code`の各要素には、改行を含まない1行の行動コードを記載させます。`await`などの実行用の記述はプログラムが補うため、同梱の生成規則を維持してください。

話し方を変える場合も、通常の行動生成では`bot.chat(...)`をこの形式で返す必要があります。「文章だけで返答する」などの矛盾する指示を加えたり、`code`というキーを変更したりすると、実行できなくなります。新しい関数名をプロンプトへ書き足すだけでは、その操作を実行できるようにはなりません。

## 7. 再起動して変更を確認する

プロンプトは**エージェントの起動時に読み込まれます**。ファイルを保存しただけでは、起動中のSallyには反映されません。

### 7.1 Sallyを再起動する

1. 編集したプロンプトと`src/sally_config.py`を保存します。モードを変更した場合は`src/runtime_settings.py`も保存します。
2. Minecraftサーバーや必要な音声サービスが、使用するモードに合わせて起動していることを確認します。
3. **EMIPフォルダ → individual_launchers → start_sally_agent.bat**をダブルクリックします。同梱BATは、既存の`emip-sally`コンテナがあれば停止・削除してから、新しく起動します。
4. SallyがMinecraftへ参加し、必要な音声接続が完了するまで待ちます。

プロンプトだけの変更であれば、Minecraftサーバーを再起動する必要はありません。BATを使わずに起動している場合も、Sallyのプロセスを終了して、普段と同じコマンドで起動し直してください。

### 7.2 使用中のモードで試す

| モード | 確認方法 |
| --- | --- |
| `none`＋`enter` | MinecraftのチャットでSallyに話しかけ、Sallyの起動ウィンドウでEnterを押します。 |
| `asr_tts`＋`enter` | 音声またはチャットで話しかけ、話し終えてから、Sallyの起動ウィンドウでEnterを押します。 |
| `gpt_live`＋`enter` | 音声で話しかけます。会話の方針は質問への返答で、行動の方針はゲーム内作業を依頼して確認します。 |
| `none`または`asr_tts`＋`decision_api` | 依頼や挨拶を送り、行動生成が始まるかを確認します。判定用プロンプトの条件を試す場合は、反応してほしい発言と反応してほしくない発言をそれぞれ使います。 |

短い返答を指示した例では、「Sally、今いる場所を教えて」と聞いて確認します。同じ条件で変更前後を比べ、一度に変更する方針を少なくすると、どの指示が影響したかを確認しやすくなります。

### 7.3 読み込まれた文章をログで確認する

通常の行動生成では、応答の解析が成功すると、同梱設定では`src/logs/日時/`に次のファイルが保存されます。最新の実行に対応する日時フォルダを開いてください。

| ファイル | 確認する内容 |
| --- | --- |
| `llm_日時_prompt.txt` | LLMへ渡したプロンプト。追記した文章や、展開後の観測情報を確認します。 |
| `llm_日時_response.txt` | LLMの応答。出力形式や、生成された発言・行動を確認します。 |
| `sally.log` | Sallyの処理ログ。エラーや、GPT-Liveへ送った観測通知などを確認します。 |

例えば、[3](#3-通常の行動生成のプロンプトを変更する)の追記後なら、`llm_日時_prompt.txt`内で「発言と協力の方針」を検索します。文章が入っていれば、編集したテンプレートが行動生成の入力へ反映されています。

GPT-Liveとの会話だけでは、行動生成用の`llm_...`ファイルが出ない場合があります。GPT-Liveへ送った観測通知は、`sally.log`内の`GPT-Live text sent`で確認できます。これは送信したテキストの記録であり、初期の会話指示全体を保存したログではありません。会話用ファイルは、参照先と再起動を確認したうえで、実際の返答でも確かめてください。

## 8. 元のプロンプトへ戻す

コピーを使って編集していれば、`src/sally_config.py`の参照先を元のファイルへ戻し、Sallyを再起動します。

| 設定のキー | 同梱の参照先 |
| --- | --- |
| `human`内の`generate_action` | `./prompts/coding_llm_human_prompt_template_jp.txt` |
| `system`内の`primitive` | `./prompts/coding_llm_system_prompt_template_jp.txt` |
| `system`内の`gpt_live` | `./prompts/gpt_live_instructions.txt` |
| `human`内の`delegated_actions` | `./prompts/delegated_actions_prompt.txt` |
| `human`内の`decision` | `./prompts/decision_prompt_template_jp.txt` |

試すためにモードを変更した場合は、`runtime_settings.py`も元の指定へ戻します。コピーしたプロンプトは、参照を外せば使用されないため、比較用に残せます。

## 9. うまく反映されないとき

| 症状 | 確認すること |
| --- | --- |
| 編集しても返答が変わらない | 使用中のモードに合うファイルか、`sally_config.py`がそのファイルを参照しているか、保存後にSallyを再起動したかを確認します。 |
| コピーしたファイルが使われない | `./prompts/...`の綴りと拡張子を確認します。読み込み先がファイルとして見つからない場合、設定文字列自体がプロンプトとして扱われるため、ファイル未検出のエラーが出るとは限りません。 |
| 起動時に`SyntaxError`が出る | 設定ファイルで、パス以外の引用符・カンマ・括弧を消していないか確認します。 |
| `UndefinedError`やテンプレートの構文エラーが出る | 追加した変数や関数が、そのテンプレートで使えるか確認します。括弧・引用符・繰り返しの終了表記も確認します。 |
| 日本語が文字化けする、読み込みに失敗する | プロンプトをUTF-8で保存し直します。 |
| GPT-Liveの話し方が変わらない | `generate_action`だけでなく、会話用の`gpt_live`を変更しているか確認します。 |
| GPT-Liveに観測情報の後半が伝わらない | `# あなたのタスク`の位置と、観測通知の文字数上限を確認します。 |
| 自動判定の条件が変わらない | `DECISION_MODE`が`decision_api`か、対象エージェントに`human`内の`decision`の設定があるかを確認します。 |
| 行動コードの解析に失敗する | `primitive`のJSON出力形式と生成規則を維持しているか、矛盾する指示を追加していないかを確認します。 |
| Anneの振る舞いも変わった | 共有ファイルを直接編集していないか確認します。エージェントごとにファイルをコピーし、それぞれの設定から参照します。 |
