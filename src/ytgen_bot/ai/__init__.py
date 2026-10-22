"""The AI layer: kie.ai chat completions behind a validator, limits and a circuit breaker.

PROVIDER NOTE (interim MVP choice): the provider/model (kie.ai, `LLM_MODEL`, currently gemini-3-8-flash-openai), the
fact that there is a SINGLE provider and NO failover, are strictly interim. The YouTubeGen core has not yet been profiled
or debugged for availability and cost; reducing LLM cost and improving availability is the planned next task, after
which the provider/model/failover decision must be revisited.
"""
