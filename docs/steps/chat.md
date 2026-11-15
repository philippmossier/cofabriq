# LLM call

One OpenAI call whose answer is parsed into a typed schema (triage card, rerank, duplicate verdict).

## Why

Every LLM answer cofabriq acts on is structured output: a Pydantic model the
response must match, so code checks it instead of parsing prose. The model
proposes, code decides: a quoted piece of evidence must really appear in the
text the model saw, and a number the model invents is dropped.

## Code

`cofabriq.llm.structured`
