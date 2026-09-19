# Whatsapp-ealtime_chatbot

whatsapp-realtime-chatbot/

├── README.md            ← full setup guide, config table, production notes
├── requirements.txt
├── pytest.ini
├── .env.example
├── .gitignore
├── app/
│   ├── __init__.py
│   ├── __main__.py      ← python -m app
│   ├── config.py
│   ├── llm.py           ← {OpenAI / Anthropic / echo providers}
│   ├── main.py          ← FastAPI routes + Bot
│   ├── store.py         ← SQLite history + idempotency
│   └── whatsapp.py      ← payload parsing, signature check, Graph sender
└── tests/
    ├── test_chatbot.py
    └── test_whatsapp_send.py
