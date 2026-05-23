.PHONY: test eval

test:
	uv run --with pytest --with httpx --with pydantic pytest tests

eval:
	uv run --with pydantic evals/run_evals.py
