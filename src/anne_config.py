from modules.config_dataclass import AgentConfig


TRACK_OTHER_BELIEFS = True

AGENT_CFG = AgentConfig(
    name="anne",
    type="BotPlayer",
    speaker_id=23,
    prompts = {
        "other_belief": "{{ events() }}",
        "human":{
                "delegated_actions": "./prompts/delegated_actions_prompt.txt",
                "generate_action": "./prompts/coding_llm_human_prompt_template_jp.txt",
            },
        "system":{
                "gpt_live": "./prompts/gpt_live_instructions.txt",
                "primitive": "./prompts/coding_llm_system_prompt_template_jp.txt",
            }
        },
    initial_belief=None,
    )
