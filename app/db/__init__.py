import os

from dotenv import load_dotenv
from sqlalchemy import create_engine

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

# pool_pre_ping : verifie qu'une connexion issue du pool est encore valide avant de
# l'utiliser -- important en serverless (Vercel) ou une connexion peut avoir ete
# fermee cote serveur entre deux invocations d'une meme instance chaude. pool_size
# reste modeste car c'est le pooler Supabase (PgBouncer, via l'URL "Session pooler")
# qui multiplexe les connexions entre les multiples instances serverless, pas
# SQLAlchemy lui-meme.
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=3, max_overflow=2)
