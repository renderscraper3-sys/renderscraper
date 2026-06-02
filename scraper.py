import asyncio
import os
import time
import io
import re
import random
import json
from datetime import datetime
from urllib.parse import urljoin
from PIL import Image
import aiohttp
from bs4 import BeautifulSoup
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# CONFIGURACIÓN Y LÍMITES
# ==========================================
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

TIEMPO_INICIO = time.time()
LIMITE_TIEMPO_SEGUNDOS = (5.5 * 3600)
CONCURRENCIA_MAXIMA = 2  
TAMANO_IMAGEN_PX = 500
BUCKET_NAME = "imagenes_scraper"
MAX_RETRIES = 3

# SEMÁFORO: Evita que Supabase colapse por demasiadas conexiones simultáneas
db_semaphore = asyncio.Semaphore(1)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
]

def obtener_headers_dinamicos():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "es-AR,es;q=0.9,en-US;q=0.8",
        "Referer": "https://www.google.com/",
        "Connection": "keep-alive"
    }

def limpiar_precio(precio_str):
    if not precio_str: return None
    limpio = re.sub(r'[^\d,.-]', '', precio_str).replace('.', '').replace(',', '.')
    try: return float(limpio)
    except ValueError: return None

# Envuelve la base de datos en el semáforo para hacer fila de a 1 petición a la vez
async def db_execute(query):
    async with db_semaphore:
        return await asyncio.to_thread(query.execute)

async def procesar_imagen(session, url_img, ean):
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
                
                # Subida de imagen también usa el semáforo implícito si es pesada, pero Storage aguanta más
                await asyncio.to_thread(
                    lambda: supabase.storage.from_(BUCKET_NAME).upload(
                        file=output.read(), path=file_path, file_options={"content-type": "image/jpeg", "upsert": "true"}
                    )
                )
                return supabase.storage.from_(BUCKET_NAME).get_public_url(file_path)
    except Exception as e:
        print(f"⚠️ Error procesando imagen EAN {ean}: {e}")
    return None

async def extraer_datos_producto(session, url):
    for intento in range(MAX_RETRIES):
        try:
            await asyncio.sleep(random.uniform(2.0, 4.0)) 
            async with session.get(url, headers=obtener_headers_dinamicos(), timeout=20) as r:
                if r.status == 200:
                    html = await r.text()
                    soup = BeautifulSoup(html, "html.parser")
                    
                    nombre = soup.find("h1").get_text(strip=True) if soup.find("h1") else None
                    precio_tag = soup.find(class_=re.compile("precio", re.I))
                    precio = precio_tag.get_text(strip=True) if precio_tag else None
                    
                    ean = None
                    script_flix = soup.find("script", attrs={"data-flix-ean": True})
                    if script_flix: ean = (script_flix.get("data-flix-ean") or "").strip() or None
                    if not ean: return None
                        
                    imagen_url_original = None
                    lista_categorias = []
                    
                    # 1. Extracción de Imagen y Categorías vía JSON-LD
                    for script in soup.find_all("script", type="application/ld+json"):
                        try:
                            txt = script.string or script.get_text(strip=True)
                            if not txt: continue
                            data = json.loads(txt)
                            objetos = data if isinstance(data, list) else [data]
                            
                            for obj in objetos:
                                 if isinstance(obj, dict) and obj.get("@type") == "Product" and obj.get("image"):
                                     img_data = obj.get("image")
                                     if not imagen_url_original:
                                         imagen_url_original = img_data if isinstance(img_data, str) else (img_data[0] if isinstance(img_data, list) and img_data else None)
                                 
                                 if isinstance(obj, dict) and obj.get("@type") == "BreadcrumbList":
                                     items = obj.get("itemListElement", [])
                                     lista_categorias = [item.get("name") for item in sorted(items, key=lambda x: int(x.get("position", 0)))]
                        except Exception: continue
                            
                    # Fallback Imagen
                    if not imagen_url_original:
                        a_foto = soup.find("a", attrs={"data-fancybox": "fotos"})
                        if a_foto and a_foto.get("href"): imagen_url_original = a_foto["href"]
                        
                    # LÓGICA DE CORTE DE CATEGORÍAS (Separación por ">")
                    # Si vino todo pegado en 1 solo texto, o si hay strings sucios
                    categorias_limpias = []
                    for cat in lista_categorias:
                        if cat:
                            # Corta por ">", quita espacios en blanco, y agrega a la lista final
                            categorias_limpias.extend([x.strip() for x in str(cat).split(">") if x.strip()])
                            
                    # Quitar el molesto "Inicio" o "Home"
                    if categorias_limpias and categorias_limpias[0].lower() in ["inicio", "home"]:
                        categorias_limpias = categorias_limpias[1:]
                        
                    cat1 = categorias_limpias[0] if len(categorias_limpias) > 0 else None
                    cat2 = categorias_limpias[1] if len(categorias_limpias) > 1 else None
                    cat3 = categorias_limpias[2] if len(categorias_limpias) > 2 else None
                    
                    return {
                        "ean": ean, "nombre": nombre, "url_producto": url,
                        "precio_actual": limpiar_precio(precio), "precio_anterior": None, 
                        "imagen_url_original": imagen_url_original,
                        "cat1": cat1, "cat2": cat2, "cat3": cat3
                    }
                elif r.status in [403, 429]:
                    print(f"🛑 Bloqueo detectado al producto ({r.status}). Pausando 30s...")
                    await asyncio.sleep(30)
        except Exception:
            await asyncio.sleep(5)
    return None

async def procesar_url_pendiente(session, row):
    url = row['url']
    print(f"\n🔍 Evaluando HTML: {url}")
    await db_execute(supabase.table("sitemap_urls").update({"ultimo_intento": datetime.now().isoformat()}).eq("id", row['id']))
    
    try:
        if "/art_" in url:
            # Lógica de Producto
            datos = await extraer_datos_producto(session, url)
            if datos:
                prod_existente = await db_execute(supabase.table("productos_laanonima").select("imagen, imagen_url").eq("ean", datos["ean"]))
                url_img_storage = None
                
                if prod_existente.data and prod_existente.data[0].get("imagen"):
                    url_img_storage = prod_existente.data[0].get("imagen_url")
                else:
                    if datos.get("imagen_url_original"):
                        url_img_storage = await procesar_imagen(session, datos["imagen_url_original"], datos["ean"])

                producto_data = {
                    "ean": datos["ean"], 
                    "nombre": datos["nombre"], 
                    "url_producto": datos["url_producto"],
                    "cat1": datos["cat1"], 
                    "cat2": datos["cat2"], 
                    "cat3": datos["cat3"],
                    "imagen": bool(url_img_storage), 
                    "imagen_url": url_img_storage, 
                    "ultima_actualizacion": datetime.now().isoformat()
                }
                res_prod = await db_execute(supabase.table("productos_laanonima").upsert(producto_data, on_conflict="ean"))
                
                if res_prod.data:
                    await db_execute(supabase.table("precios_historial").insert({
                        "producto_id": res_prod.data[0]['id'], "precio": datos["precio_actual"], "precio_anterior": datos["precio_anterior"]
                    }))
                    print(f"✅ Guardado: {datos['nombre'][:30]}... | Cats: [{datos['cat1']}] - [{datos['cat2']}] | ${datos['precio_actual']}")

        else:
            # Lógica de Categoría
            async with session.get(url, headers=obtener_headers_dinamicos(), timeout=30) as r:
                if r.status == 200:
                    soup = BeautifulSoup(await r.text(), "html.parser")
                    
                    productos = soup.select('a[data-codigo][href*="/art_"]')
                    urls_productos = [urljoin(url, a.get("href", "").strip()) for a in productos if a.get("href")]
                    
                    if urls_productos:
                        print(f"🛒 Agregando {len(urls_productos)} productos a la cola...")
                        for i in range(0, len(urls_productos), 100):
                            lote = [{"url": u, "procesado": False} for u in urls_productos[i:i+100]]
                            await db_execute(supabase.table("sitemap_urls").upsert(lote, on_conflict="url", ignore_duplicates=True))
                    
                    # Paginación
                    a_next = soup.find("a", rel=lambda v: v and "next" in v.lower() if isinstance(v, str) else False)
                    if not a_next:
                        for a in soup.find_all("a", href=True):
                            if " ".join(a.get_text(" ", strip=True).split()).lower() in {"siguiente", "next", ">", "»"}:
                                a_next = a
                                break
                    if a_next and a_next.get("href"):
                        await db_execute(supabase.table("sitemap_urls").upsert([{"url": urljoin(url, a_next["href"]), "procesado": False}], on_conflict="url", ignore_duplicates=True))

        # Marcar procesado
        await db_execute(supabase.table("sitemap_urls").update({"procesado": True}).eq("id", row['id']))
        
    except Exception as e:
        print(f"❌ Error procesando {url}: {e}")

async def orquestador():
    print("🚀 Iniciando Obrero HTML Nube...")
    
    connector = aiohttp.TCPConnector(limit_per_host=CONCURRENCIA_MAXIMA)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            if time.time() - TIEMPO_INICIO > LIMITE_TIEMPO_SEGUNDOS:
                print("\n⏰ Límite de Github alcanzado. Cerrando limpiamente.")
                break
                
            # IMPORTANTE: `desc=True` procesa los productos recién agregados ANTES que el resto de categorías
            res = await db_execute(supabase.table("sitemap_urls").select("*").eq("procesado", False).order("id", desc=True).limit(5))
            
            if not res.data:
                print("🏁 No hay más URLs pendientes en Supabase. Proceso completo.")
                break
                
            tareas = [procesar_url_pendiente(session, row) for row in res.data]
            await asyncio.gather(*tareas)

if __name__ == "__main__":
    asyncio.run(orquestador())
