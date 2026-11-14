# EMIP — Changing Prompts

[日本語](./README.prompts.ja.md)

**This README is for users who can already run EMIP and want to change an agent's speaking style, action policy, or the information passed to the LLM.**

For initial setup and voice connections, see the [Getting Started README](./README.md). This guide uses Sally's configuration to explain how to copy and edit a prompt, change the file it loads, and try the result.

| What you want to do | Where to start |
| --- | --- |
| Check the active mode and choose a file to edit | [1. Check the Active Mode](#1-check-the-active-mode), [2. Choose a File to Edit](#2-choose-a-file-to-edit) |
| Change speech and action policies for regular action generation | [3. Change the Action Generation Prompt](#3-change-the-action-generation-prompt) |
| Change how GPT-Live talks | [4. Change the GPT-Live Conversation Prompt](#4-change-the-gpt-live-conversation-prompt) |
| Change when automatic decisions trigger action generation | [5. Change the Automatic Decision Question](#5-change-the-automatic-decision-question) |
| Change the content or order of observation information | [6. Edit Templates](#6-edit-templates) |
| Apply and check your changes | [7. Restart and Check Your Changes](#7-restart-and-check-your-changes) |

## 1. Check the Active Mode

Different prompts are used in different modes. Before editing, open **EMIP folder → src → runtime_settings.py** in File Explorer and check `VOICE_MODE` and `DECISION_MODE`. The EMIP folder is the folder containing `docker-compose.yml`.

### Voice mode

| Setting in `runtime_settings.py` | Mode | Main prompts used |
| --- | --- | --- |
| `VOICE_MODE = VOICE_MODE_LIST[0]` | `none`: no voice | `generate_action` and `primitive` for action generation |
| `VOICE_MODE = VOICE_MODE_LIST[1]` | `asr_tts`: speech recognition and speech synthesis | `generate_action` and `primitive` for action generation. Generated chat messages are converted to speech. |
| `VOICE_MODE = VOICE_MODE_LIST[2]` | `gpt_live`: direct voice conversation | `gpt_live` for conversation. Requests for in-game actions also use the action generation prompts and `delegated_actions`. |

### Action trigger mode

| Setting in `runtime_settings.py` | Behavior |
| --- | --- |
| `DECISION_MODE = DECISION_MODE_LIST[0]` | `enter`. With `asr_tts` or `none`, press Enter in Sally's window to generate actions. With `gpt_live`, actions are triggered by requests from the voice conversation system. |
| `DECISION_MODE = DECISION_MODE_LIST[1]` | `decision_api`. An API decides whether action generation is needed. Use this with `asr_tts` or `none`. |

When using GPT-Live, set `DECISION_MODE` to `enter`. In this combination, you do not use the regular Enter input to advance conversations or actions.

The Getting Started README covers the combination of `asr_tts` and `enter`. If you have changed the settings, choose the examples below based on **the modes currently specified in your configuration file**.

## 2. Choose a File to Edit

Sally's prompt paths are grouped under `AGENT_CFG.prompts` in [src/sally_config.py](./src/sally_config.py). The key `human` labels input passed to the LLM; it does not refer only to text typed directly by a human user.

### Purposes and file paths

| What you want to change | Configuration key | Bundled path, relative to `src/` |
| --- | --- | --- |
| The order of observation information, instructions for regular action generation, and speech/action policies | `prompts["human"]["generate_action"]` | [prompts/coding_llm_human_prompt_template_jp.txt](./src/prompts/coding_llm_human_prompt_template_jp.txt) |
| Available action functions, rules for generating action code, and the output format | `prompts["system"]["primitive"]` | [prompts/coding_llm_system_prompt_template_jp.txt](./src/prompts/coding_llm_system_prompt_template_jp.txt) |
| GPT-Live's speaking style and when it requests in-game actions | `prompts["system"]["gpt_live"]` | [prompts/gpt_live_instructions.txt](./src/prompts/gpt_live_instructions.txt) |
| Descriptions of conversation history and execution results passed from GPT-Live to action generation | `prompts["human"]["delegated_actions"]` | [prompts/delegated_actions_prompt.txt](./src/prompts/delegated_actions_prompt.txt) |
| The question used to decide whether action generation is needed now | `prompts["human"]["decision"]` | [prompts/decision_prompt_template_jp.txt](./src/prompts/decision_prompt_template_jp.txt) |
| How other people's Beliefs are displayed in the input | `prompts["other_belief"]` | The bundled configuration contains `{{ events() }}` directly, rather than a file path. |

`src/prompts/` also contains action generation templates without `_jp` in their names. **The file referenced by the agent configuration is the one actually used.** The program does not switch files automatically based on their names or Minecraft's display language.

Sally and Anne share several prompt files. Editing a shared file directly affects both agents the next time they start. To change only Sally, copy the file and change only Sally's reference, as shown below.

> [!NOTE]
> Prompts let you change instructions to the LLM and the structure of its input. Adding new action functions, new types of observable information, or new execution control also requires changes to the corresponding program code.

## 3. Change the Action Generation Prompt

This example adds a policy to **keep Sally's responses short and ask for clarification when a request is unclear**, using `asr_tts` or `none`. To change GPT-Live's spoken conversation style, use [Section 4](#4-change-the-gpt-live-conversation-prompt).

### 3.1 Copy the original file

1. In File Explorer, open **EMIP folder → src → prompts**.
2. Copy `coding_llm_human_prompt_template_jp.txt`.
3. Name the copy `coding_llm_human_prompt_custom_jp.txt`.
4. Open the copy in a text editor such as Notepad.

If Windows is hiding file extensions, enable **View → Show → File name extensions** in File Explorer. Check that the filename does not end in `.txt.txt`.

### 3.2 Add a policy

For example, append the following to the end of the copied file. Keep the existing observation information and output-format instructions.

```text
## Speech and Cooperation Policy
- Keep your responses to one or two short sentences, as a general rule.
- If the requested target or location is unclear, ask a brief clarification question before starting work.
- If something cannot be confirmed from your observations, say that you do not know.
- Do not say that work is complete before checking the execution result.
- Generate speech as bot.chat(...) action code, following the existing output format.
```

Save with `Ctrl + S`, using **UTF-8** encoding. This example is an addition to the existing instructions. Do not replace the entire file with this text.

### 3.3 Change Sally's file reference

Open `src/sally_config.py` in a text editor and find the following line under `human` in `prompts`.

Before:

```text
"generate_action": "./prompts/coding_llm_human_prompt_template_jp.txt",
```

After:

```text
"generate_action": "./prompts/coding_llm_human_prompt_custom_jp.txt",
```

Change only the path on this line. Keep the quotation marks, comma, and surrounding brackets. Save with `Ctrl + S`.

When launching with the bundled BAT file, paths are relative to `src/`. If the file on your PC is `EMIP folder/src/prompts/coding_llm_human_prompt_custom_jp.txt`, the configuration value is `./prompts/coding_llm_human_prompt_custom_jp.txt`.

**Checkpoint: The copied file contains your added policy, and Sally's `generate_action` points to that copy.** Continue to [7. Restart and Check Your Changes](#7-restart-and-check-your-changes).

## 4. Change the GPT-Live Conversation Prompt

Use `gpt_live_instructions.txt` to configure what GPT-Live says and its tone. In-game actions are delegated to a separate action generation process, so conversation instructions and action generation instructions have separate roles.

### 4.1 Copy and edit the conversation file

1. Copy `src/prompts/gpt_live_instructions.txt`.
2. Name the copy `gpt_live_instructions_custom.txt`.
3. For example, append the following to the end of the copy.

```text
## Conversation Policy
- Keep your responses to one or two short sentences, as a general rule.
- When asked how to do something, explain the first steps one at a time.
- Prioritize answers to questions and requests, and avoid lengthy, unsolicited descriptions of the situation.
- If you could not hear clearly or a request is ambiguous, ask a brief clarification question.
```

Keep the existing action-delegation instructions starting with `Delegation policy`, including the instruction not to claim that an action succeeded until its execution result has been confirmed. These instructions connect spoken responses to in-game actions.

Also keep `{{ agent_name }}` near the beginning of the file. It is replaced at startup with the agent name, such as `sally`. This file uses literal string replacement for that placeholder, so **preserve `{{ agent_name }}` exactly, including the spaces inside the braces**.

### 4.2 Switch the file reference

In `src/sally_config.py`, change the entry under `system` in `prompts` as follows.

Before:

```text
"gpt_live": "./prompts/gpt_live_instructions.txt",
```

After:

```text
"gpt_live": "./prompts/gpt_live_instructions_custom.txt",
```

After saving, restart Sally using [Section 7](#7-restart-and-check-your-changes). Check that `VOICE_MODE` is `gpt_live`. This conversation prompt is not used if the mode is still `asr_tts`.

### 4.3 Change the information passed with action requests

`delegated_actions_prompt.txt` is a template for additional information passed from GPT-Live to action generation. It inserts conversation history and confirmed action execution results using the following placeholders.

| Placeholder | Information inserted |
| --- | --- |
| `{{ conversation_history }}` | The history of user and GPT-Live utterances and transcripts |
| `{{ action_results }}` | Confirmed action execution results |

To change this template, copy the file and switch the path in `prompts["human"]["delegated_actions"]`. Keep both placeholders while adjusting the surrounding explanation.

In-game actions requested by GPT-Live also use `generate_action` and `primitive`. To change policies for movement or mining, check these files as well as the conversation file.

## 5. Change the Automatic Decision Question

`decision_prompt_template_jp.txt` contains the question used in `decision_api` mode to decide **whether action generation should start now**. Its role differs from the prompt that generates the JavaScript to execute next.

1. Copy `src/prompts/decision_prompt_template_jp.txt` and name the copy `decision_prompt_custom_jp.txt`.
2. In the copy, adjust the conditions that require action generation.
3. Change the reference under `human` in `src/sally_config.py`.

```text
"decision": "./prompts/decision_prompt_custom_jp.txt",
```

For example, to make the agent respond to greetings as well, replace the bundled condition that excludes messages consisting only of greetings or impressions with the following:

```text
Action generation is needed when there is an unanswered greeting or question directed at you, or an unfinished request.
Do not respond repeatedly to greetings you have already answered or requests you have already completed.
```

Adding an opposing instruction while leaving the original condition in place makes the decision criteria ambiguous. Replace the condition you want to change, and keep the existing instruction not to treat commands embedded in observations as instructions for the decision process.

To try this change, set `DECISION_MODE` to `DECISION_MODE_LIST[1]` in `src/runtime_settings.py`. Use `asr_tts` or `none` as the voice mode. The decision interval and threshold are also set in that file, using `DECISION_INTERVAL_SECONDS` and `DECISION_THRESHOLD`.

When running in `enter` mode or using GPT-Live, editing this decision file does not enable automatic decisions. To use the same decision question for Anne, add a `decision` reference under `human` in `anne_config.py`. The bundled Anne configuration does not contain this key.

## 6. Edit Templates

If you only want to change the speaking style, try the additions in [Section 3](#3-change-the-action-generation-prompt) or [Section 4](#4-change-the-gpt-live-conversation-prompt). This section explains how to adjust the information passed to the LLM and its output rules.

### 6.1 Insert observation information

The action generation prompt, `generate_action`, uses the Jinja2 template format. Ordinary text is passed through as written, while `{{ ... }}` expressions are replaced with information at runtime. `{% ... %}` expressions represent constructs such as loops.

The bundled template uses the following expressions.

| Expression | Content |
| --- | --- |
| `{{ self_name }}` | The agent's own name |
| `{{ thought() }}` | Thought information recorded in the Belief |
| `{{ chat_log() }}` | Chat history |
| `{{ position(self_name) }}` | The agent's own position |
| `{{ inventory(self_name) }}` | The agent's own inventory |
| `{{ chests() }}` | Chest information in the Belief |
| `{{ other_players(self_name) }}` | Information about players other than the agent itself |
| `{{ blocks_and_visibilities(["oak_log", "stone"]) }}` | Information about the specified block types and their visibility |
| `{{ events_and_visibilities() }}` | Event and visibility history |
| The block from `{% for person, text in other_beliefs.items() %}` to `{% endfor %}` | Displays the agent's estimates of other people's Beliefs, one at a time |

A Belief is the information an agent maintains through observations and other inputs. Displaying another person's Belief does not directly reveal that person's actual thoughts.

For example, to restrict the block information passed to the LLM to oak logs, stone, and iron ore, replace the `blocks_and_visibilities(...)` line in your copied action generation template with:

```text
{{ blocks_and_visibilities(["oak_log", "stone", "iron_ore"]) }}
```

This list selects which information already held by the agent is displayed in the prompt. It does not expand the observation range or the information the agent can acquire.

Keep parentheses, quotation marks, and paired constructs such as `{% endfor %}` intact. Adding an undefined variable or function name causes an error when the template is rendered.

### 6.2 How template syntax differs between files

| Prompt being edited | How expressions are handled |
| --- | --- |
| `generate_action` | Evaluates the observation functions in the table above and variables such as `self_name`. |
| `delegated_actions` | Replaces `{{ conversation_history }}` and `{{ action_results }}` with their values. |
| `gpt_live` | Replaces `{{ agent_name }}` with the name. It does not evaluate other templates' variables or observation functions. |
| `primitive`, `decision` | Uses the text as written. Adding `{{ ... }}` does not insert observation information. |

`other_belief` specifies how another person's Belief is displayed as text. The bundled `{{ events() }}` setting displays events in that Belief. Also check `TRACK_OTHER_BELIEFS` in the agent configuration to see whether other people's Beliefs are tracked. In the input for automatic decisions, the collection of other people's Beliefs is treated as empty.

### 6.3 Headings also used by GPT-Live

In addition to regular action generation, `generate_action` is used for observation updates sent to GPT-Live and for automatic decision input. Removing or reordering information also changes these inputs.

For GPT-Live, the program specifically reads the following headings. The Japanese text shown here is part of the recognized syntax; keep it in Japanese.

| Heading | How GPT-Live uses it |
| --- | --- |
| `# あなたのタスク` (Your Task) | The text before this heading is used for observation updates. The heading and subsequent action generation instructions are excluded from those updates. |
| `## 目的` (Goal), `## 行動方針` (Action Policy) | If these sections are present, they are separated from observation updates and added to GPT-Live's instructions at startup. |

Changing these headings changes how the text is divided. Keep the existing `# あなたのタスク` heading in its original wording and position. Use `## 目的` and `## 行動方針` when you intend to share those sections in this way. Each section ends at the next heading. When these sections are added to GPT-Live's initial instructions, only `self_name` and `other_name` are supplied as template variables. Put observation functions in the part that displays observation information.

Observation updates sent to GPT-Live have a character limit set by `GPT_LIVE_SETTINGS.observation_character_limit` in `runtime_settings.py`. With a long template, later content may be omitted from the update.

### 6.4 Preserve the action code output format

To change `primitive`, copy the original file and point `prompts["system"]["primitive"]` in `sally_config.py` to that copy.

This file describes the available action functions and output rules. The current action execution process accepts **JSON containing a `code` array**, such as:

```json
{
  "code": [
    "bot.chat('Hello!')"
  ]
}
```

Each element of `code` must contain a single line of action code without line breaks. The program adds execution syntax such as `await`, so keep the bundled generation rules.

Even when changing the speaking style, regular action generation must return `bot.chat(...)` in this format. Adding contradictory instructions such as "reply with plain text only," or changing the `code` key, prevents execution. Writing a new function name into the prompt does not make that operation available.

## 7. Restart and Check Your Changes

Prompts are **loaded when the agent starts**. Saving a file alone does not update a running Sally.

### 7.1 Restart Sally

1. Save your edited prompt and `src/sally_config.py`. If you changed the mode, also save `src/runtime_settings.py`.
2. Check that the Minecraft server and the voice services required for your mode are running.
3. Double-click **EMIP folder → individual_launchers → start_sally_agent.bat**. If an `emip-sally` container already exists, the bundled BAT file stops and removes it before starting a new one.
4. Wait for Sally to join Minecraft and for any required voice connections to complete.

You do not need to restart the Minecraft server for prompt-only changes. If you launch without a BAT file, stop Sally's process and start it again using your usual command.

### 7.2 Try it in the active mode

| Mode | How to check |
| --- | --- |
| `none` + `enter` | Address Sally in Minecraft chat, then press Enter in Sally's launch window. |
| `asr_tts` + `enter` | Address Sally by voice or chat. After you finish speaking or sending your message, press Enter in Sally's launch window. |
| `gpt_live` + `enter` | Speak to Sally. Check the conversation policy through answers to questions, and the action policy by requesting an in-game task. |
| `none` or `asr_tts` + `decision_api` | Send a request or greeting and check whether action generation starts. To test decision prompt conditions, try both messages that should trigger a response and messages that should not. |

For the short-response example, ask, "Sally, tell me where you are now." Compare behavior before and after the change under the same conditions. Changing only a few policy points at a time makes it easier to see which instruction affected the result.

### 7.3 Check the loaded text in the logs

For regular action generation, the following files are saved after the response is successfully parsed. With the bundled settings, they are stored in `src/logs/<timestamp>/`. Open the timestamped folder for the latest run.

| File | What to check |
| --- | --- |
| `llm_<timestamp>_prompt.txt` | The prompt passed to the LLM. Check your added text and the rendered observation information. |
| `llm_<timestamp>_response.txt` | The LLM's response. Check the output format and generated speech/actions. |
| `sally.log` | Sally's process log. Check errors and observation updates sent to GPT-Live. |

For example, after making the addition in [Section 3](#3-change-the-action-generation-prompt), search for "Speech and Cooperation Policy" in `llm_<timestamp>_prompt.txt`. If the text is present, your edited template has been included in the action generation input.

A conversation with GPT-Live alone may not produce any `llm_...` action generation files. To check observation updates sent to GPT-Live, search for `GPT-Live text sent` in `sally.log`. This records text that was sent; it does not store the complete initial conversation instructions. For the conversation file, check the configured path, restart the agent, and also verify the result through actual responses.

## 8. Restore the Original Prompts

If you edited copies, point the references in `src/sally_config.py` back to the original files and restart Sally.

| Configuration key | Bundled path |
| --- | --- |
| `generate_action` under `human` | `./prompts/coding_llm_human_prompt_template_jp.txt` |
| `primitive` under `system` | `./prompts/coding_llm_system_prompt_template_jp.txt` |
| `gpt_live` under `system` | `./prompts/gpt_live_instructions.txt` |
| `delegated_actions` under `human` | `./prompts/delegated_actions_prompt.txt` |
| `decision` under `human` | `./prompts/decision_prompt_template_jp.txt` |

If you changed the mode for testing, restore the original settings in `runtime_settings.py` as well. Once the copied prompts are no longer referenced, they are not used, so you can keep them for comparison.

## 9. Troubleshooting

| Symptom | What to check |
| --- | --- |
| Responses do not change after editing | Check that you edited the file for the active mode, that `sally_config.py` points to it, and that you restarted Sally after saving. |
| The copied file is not used | Check the spelling and extension in `./prompts/...`. If the path does not resolve to a file, the configuration string itself is treated as the prompt, so a missing-file error may not appear. |
| A `SyntaxError` occurs at startup | Check that you did not remove quotation marks, commas, or brackets around the path in the configuration file. |
| An `UndefinedError` or template syntax error occurs | Check that added variables and functions are available in that template. Also check parentheses, quotation marks, and loop-closing expressions. |
| Japanese text is garbled, or the file cannot be read | Save the prompt again using UTF-8 encoding. |
| GPT-Live's speaking style does not change | Check that you edited the conversation prompt, `gpt_live`, rather than only `generate_action`. |
| GPT-Live does not receive the later observation information | Check the position of `# あなたのタスク` and the character limit for observation updates. |
| Automatic decision conditions do not change | Check that `DECISION_MODE` is `decision_api` and that the target agent has a `decision` entry under `human`. |
| Parsing action code fails | Check that you preserved `primitive`'s JSON output format and generation rules, and did not add contradictory instructions. |
| Anne's behavior changed too | Check whether you edited a shared file directly. Make a separate copy for each agent and reference it from that agent's configuration. |
