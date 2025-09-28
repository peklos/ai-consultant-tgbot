import os
import re
import asyncio
import logging
import aiohttp
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
import psycopg2
from psycopg2.extras import DictCursor

load_dotenv()

TG_TOKEN = os.getenv("TG_TOKEN")
AI_API_KEY = os.getenv("AI_API_KEY")
DB_DSN = {
    "host": os.getenv("DB_HOST", '127.0.0.1'),
    "port": int(os.getenv("DB_PORT", 5432)),
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASS")
}
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", '0'))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=TG_TOKEN)
dp = Dispatcher()

AI_API_URL = "https://api.intelligence.io.solutions/api/v1/chat/completions"
AI_MODEL = "deepseek-ai/DeepSeek-R1-0528"


def get_db_connection_sync():
    conn = psycopg2.connect(
        host=DB_DSN['host'],
        port=DB_DSN['port'],
        dbname=DB_DSN['dbname'],
        user=DB_DSN['user'],
        password=DB_DSN['password']
    )
    return conn


async def run_db_sync(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


def search_products_sync(query: str, max_results: int = 5, price_max: int = None):
    q = f"%{query.lower()}%"
    conn = get_db_connection_sync()
    cur = conn.cursor(cursor_factory=DictCursor)

    if price_max is not None:
        cur.execute("""SELECT * FROM products WHERE (LOWER(name) ILIKE %s OR LOWER(description) ILIKE %s)
                      AND price <= %s
                      ORDER BY price ASC
                      LIMIT %s
                      """, (q, q, price_max, max_results))
    else:
        cur.execute("""SELECT * FROM products
                      WHERE LOWER(name) ILIKE %s OR LOWER(description) ILIKE %s
                      ORDER BY price ASC 
                      LIMIT %s""", (q, q, max_results))

    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]


def save_message_sync(user_id: int, user_msg: str, bot_resp: str):
    conn = get_db_connection_sync()
    cur = conn.cursor()
    cur.execute('INSERT INTO messages (user_id, user_message, bot_response) VALUES (%s, %s, %s)',
                (user_id, user_msg, bot_resp))
    conn.commit()
    cur.close()
    conn.close()


async def ask_ai_api(system_prompt: str, user_prompt: str):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {AI_API_KEY}"
    }

    data = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(AI_API_URL, headers=headers, json=data) as response:

                response_text = await response.text()
                logger.info(f"API response: {response_text}")

                try:
                    result = await response.json()
                    text = result['choices'][0]['message']['content']
                    
                    cleaned_text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
                    
                    cleaned_text = cleaned_text.strip()
                    
                    return cleaned_text 
                except:
                    return response_text
    except Exception as e:
        logger.error(f'ошибка апи: {e}')
        return 'извините, произошла ошибка при обр запр'


def build_prompt(user_query: str, products: list):
    if not products:
        return f'Клиент спросил: {user_query}. У магазина нет подходящих товаров. Ответь кратко и предложи уточнить запрос.'

    items = '\n'.join(
        [f'{i+1}. {p["name"]} - {p["price"]} руб. {p.get("description", "")}' for i,
         p in enumerate(products)]
    )

    prompt = (
        "Ты — дружелюбный консультант интернет-магазина YoriShop. "
        "Внизу — список подходящих товаров. Оцени их и помоги клиенту выбрать.\n\n"
        f"Товары:\n{items}\n\n"
        f"Вопрос клиента: {user_query}\n\n"
        "Ответь простым языком, укажи плюсы/минусы каждого товара (коротко), "
        "и предложи лучший выбор (1-2 варианта) с объяснением. "
        "Если нужно — предложи альтернативы."
    )

    return prompt


def extract_max_price(text: str):
    m = re.search(r"до\s*(\d{3,7})", text.replace(' ', '').lower())
    if m:
        try:
            return int(m.group(1))
        except:
            return None

    return None


@dp.message(Command('start'))
@dp.message(Command('help'))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Я магазинный консультант.\n"
        "Напиши что-нибудь вроде: 'хочу кроссовки для бега до 8000' — я посоветую.\n"
        "Если ты админ, можешь использовать /addproduct"
    )


@dp.message()
async def handle_query(message: types.Message):
    user_text = message.text or ''
    user_id = message.from_user.id
    logger.info("User %s asked: %s", user_id, user_text)

    price_max = extract_max_price(user_text)
    products = await run_db_sync(search_products_sync, user_text, 5, price_max)

    prompt = build_prompt(user_text, products)
    system_prompt = 'Ты эксперт-консультант магазина. Отвечай по товарам ясно и кратко.'

    answer = await ask_ai_api(system_prompt, prompt)

    await run_db_sync(save_message_sync, user_id, user_text, answer)

    if len(answer) > 4000:
        answer = answer[:4000] + '\n\n(ответ укорочен)'
    await message.answer(answer)


async def main():
    logger.info('starting bot...')
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
