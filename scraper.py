import asyncio
import os
import time
import io
import re
import random
import json
import xml.etree.ElementTree as ET
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
LIMITE_TIEMPO_SEGUNDOS = (5.5 * 3600)  # 5.5 horas de límite para Github
CONCURRENCIA_MAXIMA = 2  
TAMANO_IMAGEN_PX = 500
BUCKET_NAME = "imagenes_scraper"
MAX_RETRIES = 3

# Lista fija de User-Agents confiables (fake_useragent a veces genera UAs de bots)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0"
]

def obtener_headers_dinamicos():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "es-AR,es;q=0.9,en-US;q=0.8,en;q=0.7",
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

# Helper para ejecutar llamadas de Supabase sin bloquear el event loop asíncrono
async def db_execute(query):
    return await asyncio.to_thread(query.execute)

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
                
                # Subida sincrónica enviada al threadpool
                await asyncio.to_thread(
                    lambda: supabase.storage.from_(BUCKET_NAME).upload(
                        file=output.read(), 
                        path=file_path, 
                        file_options={"content-type": "image/jpeg", "upsert": "true"}
                    )
                )
                return supabase.storage.from_(BUCKET_NAME).get_public_url(file_path)
    except Exception as e:
        print(f"⚠️ Error procesando imagen EAN {ean}: {e}")
    return None

async def extraer_datos_producto(session, url):
    """Extrae datos base y URL de imagen del HTML del producto."""
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
                    if script_flix:
                        ean = (script_flix.get("data-flix-ean") or "").strip() or None
                    
                    if not ean: 
                        return None
                        
                    imagen_url_original = None
                    for script in soup.find_all("script", type="application/ld+json"):
                        try:
                            txt = script.string or script.get_text(strip=True)
                            if not txt: continue
                            data = json.loads(txt)
                            objetos = data if isinstance(data, list) else [data]
                            for obj in objetos:
                                 if isinstance(obj, dict) and obj.get("@type") == "Product" and obj.get("image"):
                                     img_data = obj.get("image")
                                     imagen_url_original = img_data if isinstance(img_data, str) else (img_data[0] if isinstance(img_data, list) and img_data else None)
                                     break
                        except Exception: continue
                            
                    if not imagen_url_original:
                        a_foto = soup.find("a", attrs={"data-fancybox": "fotos"})
                        if a_foto and a_foto.get("href"): imagen_url_original = a_foto["href"]
                    
                    return {
                        "ean": ean,
                        "nombre": nombre,
                        "url_producto": url,
                        "precio_actual": limpiar_precio(precio),
                        "precio_anterior": None,
                        "imagen_url_original": imagen_url_original
                    }
                elif r.status in [403, 429]:
                    print(f"🛑 Bloqueo detectado al producto ({r.status}). Pausando 30s...")
                    await asyncio.sleep(30)
        except Exception as e:
            await asyncio.sleep(5)
    return None

async def guardar_en_db(datos, url_imagen_storage):
    """Guarda o actualiza producto y añade registro al historial de precios."""
    try:
        producto_data = {
            "ean": datos["ean"],
            "nombre": datos["nombre"],
            "url_producto": datos["url_producto"],
            "imagen": bool(url_imagen_storage),
            "imagen_url": url_imagen_storage,
            "ultima_actualizacion": datetime.now().isoformat()
        }
        res_prod = await db_execute(supabase.table("productos_laanonima").upsert(producto_data, on_conflict="ean"))
        
        if res_prod.data:
            producto_id = res_prod.data[0]['id']
            historial_data = {
                "producto_id": producto_id,
                "precio": datos["precio_actual"],
                "precio_anterior": datos["precio_anterior"]
            }
            await db_execute(supabase.table("precios_historial").insert(historial_data))
            print(f"✅ Guardado: {datos['nombre'][:30]}... | EAN: {datos['ean']} | ${datos['precio_actual']}")
            
    except Exception as e:
        print(f"❌ Error guardando en BD EAN {datos['ean']}: {e}")

async def procesar_url_pendiente(session, row):
    url = row['url']
    print(f"\n🔍 Evaluando: {url}")
    
    await db_execute(supabase.table("sitemap_urls").update({"ultimo_intento": datetime.now().isoformat()}).eq("id", row['id']))
    
    try:
        if ".xml" in url:
            # ES UN SITEMAP
            async with session.get(url, headers=obtener_headers_dinamicos(), timeout=30) as r:
                print(f"📡 Estado HTTP Sitemap: {r.status}")
                xml_text = await r.text()
                
                # Logs requeridos para depuración
                print(f"📄 Primeros 400 caracteres de la respuesta:\n{xml_text[:400]}")
                
                if r.status == 200:
                    # Limpiamos namespaces (xmlns) del XML para un parseo a prueba de fallos
                    xml_clean = re.sub(r'\sxmlns="[^"]+"', '', xml_text, count=1)
                    root = ET.fromstring(xml_clean)
                    
                    # Busca locs sin importar el namespace
                    urls_encontradas = [elem.text.strip() for elem in root.findall(".//loc") if elem.text]
                    
                    if urls_encontradas:
                        print(f"📦 ¡Jackpot! {len(urls_encontradas)} URLs encontradas. Mandando a la cola...")
                        for i in range(0, len(urls_encontradas), 100):
                            lote = [{"url": u, "procesado": False} for u in urls_encontradas[i:i+100]]
                            await db_execute(supabase.table("sitemap_urls").upsert(lote, on_conflict="url", ignore_duplicates=True))
                    else:
                        print("⚠️ No se encontraron etiquetas <loc> en el XML.")
                else:
                    print(f"❌ Error al obtener el sitemap. Es posible que el servidor esté bloqueando la petición.")

        elif "/art_" in url:
            # ES UN PRODUCTO
            datos = await extraer_datos_producto(session, url)
            if datos:
                prod_existente = await db_execute(supabase.table("productos_laanonima").select("imagen, imagen_url").eq("ean", datos["ean"]))
                url_img_storage = None
                
                if prod_existente.data and prod_existente.data[0].get("imagen"):
                    url_img_storage = prod_existente.data[0].get("imagen_url")
                    print(f"⏭️ Imagen ya existe en Supabase para EAN {datos['ean']}.")
                else:
                    if datos.get("imagen_url_original"):
                        url_img_storage = await procesar_imagen(session, datos["imagen_url_original"], datos["ean"])

                await guardar_en_db(datos, url_img_storage)

        else:
            # ES UNA CATEGORÍA
            async with session.get(url, headers=obtener_headers_dinamicos(), timeout=30) as r:
                if r.status == 200:
                    html = await r.text()
                    soup = BeautifulSoup(html, "html.parser")
                    
                    productos = soup.select('a[data-codigo][href*="/art_"]')
                    urls_productos = [urljoin(url, a.get("href", "").strip()) for a in productos if a.get("href")]
                    
                    if urls_productos:
                        print(f"🛒 Encontrados {len(urls_productos)} productos en categoría. Mandando a cola...")
                        for i in range(0, len(urls_productos), 100):
                            lote = [{"url": u, "procesado": False} for u in urls_productos[i:i+100]]
                            await db_execute(supabase.table("sitemap_urls").upsert(lote, on_conflict="url", ignore_duplicates=True))
                    
                    a_next = soup.find("a", rel=lambda v: v and "next" in v.lower() if isinstance(v, str) else False)
                    if not a_next:
                        for a in soup.find_all("a", href=True):
                            if " ".join(a.get_text(" ", strip=True).split()).lower() in {"siguiente", "next", ">", "»"}:
                                a_next = a
                                break
                                
                    if a_next and a_next.get("href"):
                        next_url = urljoin(url, a_next["href"])
                        await db_execute(supabase.table("sitemap_urls").upsert([{"url": next_url, "procesado": False}], on_conflict="url", ignore_duplicates=True))
                else:
                    print(f"⚠️ Error {r.status} al acceder a categoría {url}")

        # Marcar URL como completada tras procesarla con éxito
        await db_execute(supabase.table("sitemap_urls").update({"procesado": True}).eq("id", row['id']))
        
    except Exception as e:
        print(f"❌ Error procesando {url}: {e}")

async def asegurar_arranque_automatico():
    """Verifica si la cola está vacía. Si lo está, inyecta el sitemap original."""
    pendientes = await db_execute(supabase.table("sitemap_urls").select("id").eq("procesado", False).limit(1))
    if not pendientes.data:
        print("⚙️ La cola está vacía. Inyectando Sitemap original para reiniciar el ciclo...")
        await db_execute(supabase.table("sitemap_urls").upsert(
            {"url": "https://www.laanonima.com.ar/sitemap-listados.xml", "procesado": False}, 
            on_conflict="url"
        ))

async def orquestador():
    print("🚀 Iniciando Scraper con Protección Anti-Ban y Carrera de Relevos...")
    
    await asegurar_arranque_automatico()
    
    connector = aiohttp.TCPConnector(limit_per_host=CONCURRENCIA_MAXIMA)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            if time.time() - TIEMPO_INICIO > LIMITE_TIEMPO_SEGUNDOS:
                print("\n⏰ Límite de 5.5 horas alcanzado. Cerrando limpiamente para pasar el relevo...")
                break
                
            res = await db_execute(supabase.table("sitemap_urls").select("*").eq("procesado", False).order("id").limit(5))
            
            if not res.data:
                print("🏁 No hay más URLs pendientes. Proceso completo.")
                break
                
            tareas = [procesar_url_pendiente(session, row) for row in res.data]
            await asyncio.gather(*tareas)

if __name__ == "__main__":
    asyncio.run(orquestador())
