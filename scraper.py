import asyncio
import os
import time
import io
import re
import random
from datetime import datetime
from PIL import Image
import aiohttp
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# CONFIGURACIÓN Y LIMITES
# ==========================================
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

TIEMPO_INICIO = time.time()
LIMITE_TIEMPO_SEGUNDOS = (5.5 * 3600)  # 5.5 horas de límite para GitHub Actions
CONCURRENCIA_MAXIMA = 2  # Bajo para no saturar y evitar bans
TAMANO_IMAGEN_PX = 500
BUCKET_NAME = "imagenes_scraper"
MAX_RETRIES = 3

ua = UserAgent()

def obtener_headers_dinamicos():
    return {
        "User-Agent": ua.random,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": random.choice(["es-AR,es;q=0.9", "es-ES,es;q=0.8,en;q=0.7", "en-US,en;q=0.5"]),
        "Referer": "https://www.google.com/",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
    }

def limpiar_precio(precio_str):
    if not precio_str:
        return None
    limpio = re.sub(r'[^\d,.-]', '', precio_str).replace('.', '').replace(',', '.')
    try:
        return float(limpio)
    except ValueError:
        return None

async def procesar_imagen(session, url_img, ean):
    """Descarga, redimensiona y sube a Supabase Storage en memoria."""
    try:
        async with session.get(url_img, headers=obtener_headers_dinamicos(), timeout=15) as r:
            if r.status == 200:
                img_data = await r.read()
                img = Image.open(io.BytesIO(img_data))
                
                if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                    bg = Image.new('RGB', img.size, (255, 255, 255))
                    bg.paste(img, mask=img.split()[3]) if img.mode == 'RGBA' else bg.paste(img)
                    img = bg
                elif img.mode != 'RGB':
                    img = img.convert('RGB')
                
                img.thumbnail((TAMANO_IMAGEN_PX, TAMANO_IMAGEN_PX), Image.Resampling.LANCZOS)
                
                output = io.BytesIO()
                img.save(output, format="JPEG", quality=85)
                output.seek(0)
                
                file_path = f"{ean}.jpg"
                
                # Subir a Supabase Storage
                supabase.storage.from_(BUCKET_NAME).upload(
                    file=output.read(), 
                    path=file_path, 
                    file_options={"content-type": "image/jpeg", "upsert": "true"}
                )
                
                return supabase.storage.from_(BUCKET_NAME).get_public_url(file_path)
    except Exception as e:
        print(f"⚠️ Error con imagen EAN {ean}: {e}")
    return None

async def extraer_datos_producto(session, url):
    """Extrae datos base del HTML del producto."""
    for intento in range(MAX_RETRIES):
        try:
            await asyncio.sleep(random.uniform(2.0, 5.0)) # Jitter anti-ban
            async with session.get(url, headers=obtener_headers_dinamicos(), timeout=20) as r:
                if r.status == 200:
                    html = await r.text()
                    soup = BeautifulSoup(html, "html.parser")
                    
                    # Logica basica de extracción (Adaptada a la estructura de La Anonima)
                    nombre = soup.find("h1").get_text(strip=True) if soup.find("h1") else None
                    precio_tag = soup.find(class_=re.compile("precio", re.I))
                    precio = precio_tag.get_text(strip=True) if precio_tag else None
                    
                    ean_script = soup.find("script", attrs={"data-flix-ean": True})
                    ean = ean_script.get("data-flix-ean") if ean_script else None
                    
                    if not ean: # Si no hay EAN, es inútil para inventario
                        return None
                        
                    return {
                        "ean": ean,
                        "nombre": nombre,
                        "url_producto": url,
                        "precio_actual": limpiar_precio(precio),
                        "precio_anterior": None # Ajustar si extraen el tachado
                    }
                elif r.status in [403, 429]:
                    print(f"🛑 Bloqueo detectado ({r.status}). Pausando 30s...")
                    await asyncio.sleep(30)
        except Exception as e:
            print(f"Error en {url}: {e}")
            await asyncio.sleep(5)
    return None

async def guardar_en_db(datos, url_imagen_storage):
    """Guarda o actualiza producto y añade registro al historial de precios."""
    try:
        # Upsert Producto
        producto_data = {
            "ean": datos["ean"],
            "nombre": datos["nombre"],
            "url_producto": datos["url_producto"],
            "imagen": bool(url_imagen_storage),
            "imagen_url": url_imagen_storage,
            "ultima_actualizacion": datetime.now().isoformat()
        }
        res_prod = supabase.table("productos_laanonima").upsert(producto_data, on_conflict="ean").execute()
        
        if res_prod.data:
            producto_id = res_prod.data[0]['id']
            
            # Insertar en Historial de Precios
            historial_data = {
                "producto_id": producto_id,
                "precio": datos["precio_actual"],
                "precio_anterior": datos["precio_anterior"]
            }
            supabase.table("precios_historial").insert(historial_data).execute()
            print(f"✅ Guardado: {datos['nombre']} | EAN: {datos['ean']} | ${datos['precio_actual']}")
            
    except Exception as e:
        print(f"❌ Error guardando en BD EAN {datos['ean']}: {e}")

async def procesar_url_pendiente(session, row):
    url = row['url']
    print(f"🔍 Procesando: {url}")
    
    # Marcamos inicio de intento
    supabase.table("sitemap_urls").update({"ultimo_intento": datetime.now().isoformat()}).eq("id", row['id']).execute()
    
    # Lógica de enrutamiento (Si es sitemap extrae URLs, si es producto extrae datos)
    if "sitemap" in url or ".xml" in url:
        # Aquí va la lógica de parsear sitemap y subir URLs nuevas a sitemap_urls con procesado=false
        # supabase.table("sitemap_urls").insert({"url": nueva_url}).execute()
        pass 
    elif "art_" in url:
        datos = await extraer_datos_producto(session, url)
        if datos:
            # Comprobar si ya tenemos imagen
            prod_existente = supabase.table("productos_laanonima").select("imagen, imagen_url").eq("ean", datos["ean"]).execute()
            url_img_storage = None
            
            if prod_existente.data and prod_existente.data[0].get("imagen"):
                url_img_storage = prod_existente.data[0].get("imagen_url")
                print(f"⏭️ Imagen existente para EAN {datos['ean']}. Saltando descarga.")
            else:
                # Extraer URL de imagen original desde HTML y descargarla
                # (Asumiendo que 'datos' trajo 'url_imagen_original')
                url_imagen_original = "..." # Aquí va el selector de imagen de BS4
                if url_imagen_original:
                    url_img_storage = await procesar_imagen(session, url_imagen_original, datos["ean"])

            await guardar_en_db(datos, url_img_storage)
    
    # Marcamos como completado
    supabase.table("sitemap_urls").update({"procesado": True}).eq("id", row['id']).execute()

async def orquestador():
    print("🚀 Iniciando Scraper con Protección Anti-Ban y Carrera de Relevos...")
    connector = aiohttp.TCPConnector(limit_per_host=CONCURRENCIA_MAXIMA)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            # Verificar Tiempo Límite GitHub Actions
            if time.time() - TIEMPO_INICIO > LIMITE_TIEMPO_SEGUNDOS:
                print("\n⏰ Límite de 5.5 horas alcanzado. Cerrando limpiamente para pasar el relevo...")
                break
                
            # Buscar siguientes 5 URLs no procesadas
            pendientes = supabase.table("sitemap_urls").select("*").eq("procesado", False).order("id").limit(5).execute()
            
            if not pendientes.data:
                print("🏁 No hay más URLs pendientes. Proceso completo.")
                break
                
            tareas = [procesar_url_pendiente(session, row) for row in pendientes.data]
            await asyncio.gather(*tareas)

if __name__ == "__main__":
    asyncio.run(orquestador())
