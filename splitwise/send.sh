curl -X POST http://0.0.0.0:8000/prefill -H "Content-Type: application/json" -d '{"max_tokens": 128, "temperature": 0.8, "top_p": 0.95, "ignore_eos": true, "prompt": "How are you? Good"}'