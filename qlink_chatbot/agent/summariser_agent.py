import os

from openai import AsyncOpenAI

from qlink_chatbot.utils.logger_config import logger

API_KEY = os.getenv("OPENAI_API_KEY")
client = AsyncOpenAI(api_key=API_KEY) if API_KEY else None

agent_summary_output_schema = {
    "format": {
        "type": "json_schema",
        "name": "agent_chat_summary",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "is_worth_storing": {
                    "type": "boolean",
                    "description": "Indicates whether the conversation contains meaningful information worth storing (True) or is just casual (False)."
                },
                "summary": {
                    "type": "string",
                    "description": "Concise semantic summary of the agent's meaningful messages, written in neutral third person. Leave empty if is_worth_storing is False."
                }
            },
            "required": [
                "is_worth_storing",
                "summary"
            ],
            "additionalProperties": False
        }
    }
}



system_prompt = """
You are a Summariser Agent for Jaipur Rugs' customer support system. 
You receive a list of all messages sent by the live agent during one chat session.

### Task:
- Review all messages together and decide if the conversation contains **any meaningful information**.
- If all messages are casual or generic (like greetings, acknowledgements, or polite replies), return:
  {
    "is_worth_storing": false,
    "summary": ""
  }
- If any message includes **useful or factual content** — such as product information, pricing, delivery, customization, return policy, instructions, or business insights — return:
  {
    "is_worth_storing": true,
    "summary": "<concise semantic summary>"
  }

### Summary Guidelines:
- Combine all relevant messages into one natural summary of 1–3 sentences.
- Focus only on **key information** or **instructions** shared by the agent.
- Ignore small talk, filler, or repeated content.
- Write in **neutral third person**, e.g., “The agent informed that…” or “The agent explained that…”.
- The summary should be clear, factual, and suitable for semantic storage.

### Output Format:
Follow this exact JSON schema:
    {
    "is_worth_storing": boolean,
    "summary": string
    }

### Output Examples:

Input:
    [
    "Hi there!",
    "Sure, I can help you with that.",
    "The wool rug collection is currently on a 15% discount till Sunday.",
    "Thank you for reaching out!"
    ]
Output:
    {
    "is_worth_storing": true,
    "summary": "The agent mentioned that the wool rug collection is currently on a 15% discount till Sunday."
    }

Input:
    [
    "Hi!",
    "Good morning!",
    "How are you today?"
    ]
Output:
    {
    "is_worth_storing": false,
    "summary": ""
    }
"""




async def summariser_agent(admin_messagaes):
    """Main Jaipur Rugs summary agent."""
    admin_messagaes = "\n".join(admin_messagaes)
    response = None

    try:
        if not client:
            raise RuntimeError("OPENAI_API_KEY is not configured.")
        input_list = [
            {"role": "user", "content": f"All the live agent texts are: {admin_messagaes}"}
        ]

        
    
        final_response = await client.responses.create(
            model="gpt-4.1-mini",
            instructions=system_prompt,
            text= agent_summary_output_schema,
            input=input_list,
        )

        logger.info(f"summary of input message is: {final_response.output_text}")
        return final_response.output_text

    except Exception as e:
        logger.error(
            "error occurred while generating summary agent response",
            extra={
                "error": str(e),
                "response": response if response else ""
            }
        )
        raise e
