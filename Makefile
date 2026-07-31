.PHONY: test eval finetune-export finetune

test:
	uv run --with pytest --with httpx --with pydantic --with openai --with 'sentry-sdk[fastapi]' --with fastapi --with 'uvicorn[standard]' pytest tests

eval:
	uv run --with pydantic --with openai --with python-dotenv evals/run_evals.py

finetune-export:
	cd scripts && uv run --with openai --with pydantic --with python-dotenv python -m school_email_pipeline.openai_finetune export

finetune:
	cd scripts && uv run --with openai --with pydantic --with python-dotenv python -m school_email_pipeline.openai_finetune train
