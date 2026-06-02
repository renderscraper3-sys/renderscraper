import asyncio
import os
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
# CONFIGURACIÓN
# ==========================================
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

TAMANO_IMAGEN_PX = 500
BUCKET_NAME = "imagenes_scraper"
MAX_RETRIES = 3

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0"
]

# URLS DE PRUEBA PROPORCIONADAS
TEST_URLS = [
    "https://www.laanonima.com.ar/tv-y-video/n1_3/",
    "https://www.laanonima.com.ar/smart-tv/n2_58/",
    "https://www.laanonima.com.ar/accesorios/n2_61/",
    "https://www.laanonima.com.ar/soportes-para-tv/n3_63/",
    "https://www.laanonima.com.ar/antenas/n3_64/",
    "https://www.laanonima.com.ar/controles-remoto/n3_65/",
    "https://www.laanonima.com.ar/lentes-tv/n3_90/",
    "https://www.laanonima.com.ar/cables/n3_66/"
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

async def db_execute(query):
    return await asyncio.to_thread(query.execute)

# ==========================================
# FUNCIONES NÚCLEO (Imagen, DB, Scraping)
# ==========================================
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
                    print(f"🛑 Bloqueo detectado al acceder al producto HTTP {r.status}.")
                    return None
        except Exception as e:
            await asyncio.sleep(2)
    return None

async def guardar_en_db(datos, url_imagen_storage):
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
            print(f"✅ ¡Éxito de BD! Guardado: {datos['nombre'][:30]}... | EAN: {datos['ean']} | ${datos['precio_actual']}")
            
    except Exception as e:
        print(f"❌ Error guardando en BD EAN {datos['ean']}: {e}")

# ==========================================
# LÓGICA DE TESTEO
# ==========================================
async def test_flujo_completo(session, url_categoria):
    print(f"\n🧪 TESTEANDO CATEGORÍA: {url_categoria}")
    try:
        async with session.get(url_categoria, headers=obtener_headers_dinamicos(), timeout=30) as r:
            print(f"📡 Estado HTTP Categoría: {r.status}")
            
            if r.status == 200:
                html = await r.text()
                soup = BeautifulSoup(html, "html.parser")
                
                productos = soup.select('a[data-codigo][href*="/art_"]')
                urls_productos = [urljoin(url_categoria, a.get("href", "").strip()) for a in productos if a.get("href")]
                
                if urls_productos:
                    print(f"✅ ÉXITO HTML: Se encontraron {len(urls_productos)} productos en esta categoría.")
                    
                    # Extraemos 1 solo producto de prueba para no abusar y ver si funciona el flujo
                    url_prueba_producto = urls_productos[0]
                    print(f"🛒 Extrayendo datos del primer producto: {url_prueba_producto}")
                    
                    datos = await extraer_datos_producto(session, url_prueba_producto)
                    if datos:
                        url_img_storage = None
                        if datos.get("imagen_url_original"):
                            print("🖼️ Intentando descargar y procesar imagen...")
                            url_img_storage = await procesar_imagen(session, datos["imagen_url_original"], datos["ean"])
                        
                        await guardar_en_db(datos, url_img_storage)
                        print(f"🎉 TEST COMPLETADO PARA: {url_categoria}")
                    else:
                        print("⚠️ Código 200 al producto, pero no se extrajeron datos válidos.")
                else:
                    print("⚠️ Código 200, pero no se encontraron enlaces de productos en el HTML. Mostrando inicio de la web:")
                    print(html[:300])
                    
            elif r.status in [403, 429]:
                print("❌ BLOQUEADO. El firewall está bloqueando el acceso a toda la web desde esta IP.")
                html = await r.text()
                print(f"📄 Respuesta:\n{html[:200]}")
            else:
                print(f"⚠️ Error inesperado HTTP {r.status}")
                
    except Exception as e:
        print(f"❌ Error de red conectando con la categoría: {e}")

async def orquestador_test():
    print("🚀 Iniciando Script de Testeo de Direcciones de La Anónima...")
    connector = aiohttp.TCPConnector(limit_per_host=1)
    async with aiohttp.ClientSession(connector=connector) as session:
        for url in TEST_URLS:
            await test_flujo_completo(session, url)
            # Pequeña pausa entre URL y URL
            await asyncio.sleep(random.uniform(3.0, 6.0))
            
    print("\n🏁 Proceso de test finalizado.")

if __name__ == "__main__":
    asyncio.run(orquestador_test())
