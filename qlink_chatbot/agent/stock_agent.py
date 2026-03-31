import json
import os

from openai import OpenAI

from qlink_chatbot.agent.schema import output_schema, system_prompt
from qlink_chatbot.utils.logger_config import logger

API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=API_KEY)

MODEL = "gpt-4.1"

def openai_stock_response(input):
    response = None
    try:
        response =  client.responses.create(
            model=MODEL,
            input=input,
            text=output_schema,
            instructions=system_prompt,
            temperature=1.0,
            tools=[
                {
                "type": "web_search",
                "user_location": {
                    "type": "approximate",
                    "country": "IN"
                },
                "search_context_size": "medium"
                }
            ],
            max_output_tokens=2048,
            top_p=1,
            store=False
        )

        return {
            "output": json.loads(response.output_text),
            "token_usage": {
                "input": response.usage.input_tokens,
                "output": response.usage.output_tokens,
            }
        }

    except Exception as e:
        logger.error(
            "error occurred while generating stock response",
            extra={
                "error": str(e),
                "response": response if response else ""
            }
        )
        raise e