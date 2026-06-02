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
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
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
LIMITE_TIEMPO_SEGUNDOS = 5.5 * 3600  # 5.5 horas de límite
TAMANO_IMAGEN_PX = 500
BUCKET_NAME = "imagenes_scraper"

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


# ==========================================
# MOTOR HTTP ORIGINAL (El que a vos te funcionaba)
# ==========================================
def crear_session():
    session = requests.Session()

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/137.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

    session.headers.update(HEADERS)

    retry = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session


def limpiar_precio(precio_str):
    if not precio_str:
        return None

    limpio = re.sub(r"[^\d,.-]", "", precio_str).replace(".", "").replace(",", ".")

    try:
        return float(limpio)
    except ValueError:
        return None


def procesar_imagen(session, url_img, ean):
    """Descarga, redimensiona y sube a Supabase Storage en memoria."""
    try:
        r = session.get(url_img, timeout=15)

        if r.status_code == 200:
            img = Image.open(io.BytesIO(r.content))

            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[3]) if img.mode == "RGBA" else bg.paste(img)
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")

            img.thumbnail((TAMANO_IMAGEN_PX, TAMANO_IMAGEN_PX), Image.Resampling.LANCZOS)

            output = io.BytesIO()
            img.save(output, format="JPEG", quality=85)
            output.seek(0)

            file_path = f"{ean}.jpg"

            supabase.storage.from_(BUCKET_NAME).upload(
                file=output.read(),
                path=file_path,
                file_options={"content-type": "image/jpeg", "upsert": "true"}
            )

            return supabase.storage.from_(BUCKET_NAME).get_public_url(file_path)

    except Exception as e:
        print(f"⚠️ Error procesando imagen EAN {ean}: {e}")

    return None


def extraer_datos_producto(session, url):
    """Extrae datos base, marca, categorías y URL de imagen."""
    try:
        r = session.get(url, timeout=20)

        if r.status_code == 403:
            print(f"🛑 Bloqueo 403 en {url}. Pausando 30s...")
            time.sleep(30)
            return None

        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")

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
            marca = None
            cat1, cat2, cat3 = None, None, None

            # Lógica para extraer Marca y Categorías del JSON oculto
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    txt = script.string or script.get_text(strip=True)
                    if not txt:
                        continue

                    data = json.loads(txt)
                    objetos = data if isinstance(data, list) else [data]

                    for obj in objetos:
                        if not isinstance(obj, dict):
                            continue

                        if obj.get("@type") == "Product":
                            if obj.get("image") and not imagen_url_original:
                                img_data = obj.get("image")
                                imagen_url_original = (
                                    img_data
                                    if isinstance(img_data, str)
                                    else (img_data[0] if isinstance(img_data, list) and img_data else None)
                                )

                            if obj.get("brand") and not marca:
                                brand_data = obj.get("brand")
                                marca = brand_data.get("name") if isinstance(brand_data, dict) else brand_data

                        if obj.get("@type") == "BreadcrumbList":
                            items = obj.get("itemListElement", [])
                            items_sorted = sorted(
                                [i for i in items if isinstance(i, dict)],
                                key=lambda x: x.get("position", 99)
                            )
                            nombres = [
                                i.get("name").strip()
                                for i in items_sorted
                                if i.get("name") and i.get("name").strip().lower() not in ["inicio", "home"]
                            ]

                            if len(nombres) > 0:
                                cat1 = nombres[0]
                            if len(nombres) > 1:
                                cat2 = nombres[1]
                            if len(nombres) > 2:
                                cat3 = nombres[2]

                except Exception:
                    continue

            if not imagen_url_original:
                a_foto = soup.find("a", attrs={"data-fancybox": "fotos"})
                if a_foto and a_foto.get("href"):
                    imagen_url_original = a_foto["href"]

            return {
                "ean": ean,
                "nombre": nombre,
                "marca": marca,
                "cat1": cat1,
                "cat2": cat2,
                "cat3": cat3,
                "url_producto": url,
                "precio_actual": limpiar_precio(precio),
                "precio_anterior": None,
                "imagen_url_original": imagen_url_original
            }

    except Exception as e:
        print(f"Error HTTP en producto {url}: {e}")

    return None


def guardar_en_db(datos, url_imagen_storage):
    """Guarda producto con todas sus categorías y añade historial de precios."""
    try:
        producto_data = {
            "ean": datos["ean"],
            "nombre": datos["nombre"],
            "marca": datos["marca"],
            "cat1": datos["cat1"],
            "cat2": datos["cat2"],
            "cat3": datos["cat3"],
            "url_producto": datos["url_producto"],
            "imagen": bool(url_imagen_storage),
            "imagen_url": url_imagen_storage,
            "ultima_actualizacion": datetime.now().isoformat()
        }

        res_prod = supabase.table("productos_laanonima").upsert(
            producto_data,
            on_conflict="ean"
        ).execute()

        if res_prod.data:
            producto_id = res_prod.data[0]["id"]

            historial_data = {
                "producto_id": producto_id,
                "precio": datos["precio_actual"],
                "precio_anterior": datos["precio_anterior"]
            }

            supabase.table("precios_historial").insert(historial_data).execute()

            cats = f"[{datos['cat1']}/{datos['cat2']}]" if datos["cat1"] else "[Sin Cat]"
            print(f"✅ Guardado: {cats} {datos['nombre'][:25]}... | EAN: {datos['ean']} | ${datos['precio_actual']}")

    except Exception as e:
        print(f"❌ Error guardando en BD EAN {datos['ean']}: {e}")


def procesar_url_pendiente(session, row):
    url = row["url"]
    print(f"🔍 Evaluando: {url}")

    supabase.table("sitemap_urls").update(
        {"ultimo_intento": datetime.now().isoformat()}
    ).eq("id", row["id"]).execute()

    try:
        if ".xml" in url:
            r = session.get(url, timeout=30)

            if r.status_code == 200:
                root = ET.fromstring(r.text)
                urls_encontradas = [
                    elemento.text.strip()
                    for elemento in root.findall(".//sm:loc", NS)
                    if elemento.text
                ]

                if urls_encontradas:
                    print(f"📦 ¡Jackpot! {len(urls_encontradas)} URLs encontradas. Mandando a la cola...")
                    for i in range(0, len(urls_encontradas), 100):
                        lote = [{"url": u, "procesado": False} for u in urls_encontradas[i:i + 100]]
                        supabase.table("sitemap_urls").upsert(
                            lote,
                            on_conflict="url",
                            ignore_duplicates=True
                        ).execute()

        elif "/art_" in url:
            datos = extraer_datos_producto(session, url)

            if datos:
                prod_existente = supabase.table("productos_laanonima").select(
                    "imagen, imagen_url"
                ).eq("ean", datos["ean"]).execute()

                url_img_storage = None

                if prod_existente.data and prod_existente.data[0].get("imagen"):
                    url_img_storage = prod_existente.data[0].get("imagen_url")
                elif datos.get("imagen_url_original"):
                    url_img_storage = procesar_imagen(
                        session,
                        datos["imagen_url_original"],
                        datos["ean"]
                    )

                guardar_en_db(datos, url_img_storage)

        else:
            r = session.get(url, timeout=30)

            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                productos = soup.select('a[data-codigo][href*="/art_"]')
                urls_productos = [
                    urljoin(url, a.get("href", "").strip())
                    for a in productos
                    if a.get("href")
                ]

                if urls_productos:
                    print(f"🛒 Encontrados {len(urls_productos)} productos en categoría. Mandando a cola...")
                    for i in range(0, len(urls_productos), 100):
                        lote = [{"url": u, "procesado": False} for u in urls_productos[i:i + 100]]
                        supabase.table("sitemap_urls").upsert(
                            lote,
                            on_conflict="url",
                            ignore_duplicates=True
                        ).execute()

                a_next = soup.find(
                    "a",
                    rel=lambda v: v and "next" in v.lower() if isinstance(v, str) else False
                )

                if not a_next:
                    for a in soup.find_all("a", href=True):
                        if " ".join(a.get_text(" ", strip=True).split()).lower() in {"siguiente", "next", ">", "»"}:
                            a_next = a
                            break

                if a_next and a_next.get("href"):
                    next_url = urljoin(url, a_next["href"])
                    supabase.table("sitemap_urls").upsert(
                        [{"url": next_url, "procesado": False}],
                        on_conflict="url",
                        ignore_duplicates=True
                    ).execute()

        supabase.table("sitemap_urls").update(
            {"procesado": True}
        ).eq("id", row["id"]).execute()

    except Exception as e:
        print(f"❌ Error procesando {url}: {e}")


def asegurar_arranque_automatico():
    pendientes = supabase.table("sitemap_urls").select("id").eq("procesado", False).limit(1).execute()

    if not pendientes.data:
        print("⚙️ La cola está vacía. Inyectando Sitemap original...")
        supabase.table("sitemap_urls").upsert(
            {"url": "https://www.laanonima.com.ar/sitemap-listados.xml", "procesado": False},
            on_conflict="url"
        ).execute()


def orquestador():
    print("🚀 Iniciando Scraper con Motor Original...")
    asegurar_arranque_automatico()
    session = crear_session()

    while True:
        if time.time() - TIEMPO_INICIO > LIMITE_TIEMPO_SEGUNDOS:
            print("\n⏰ Límite de 5.5 horas alcanzado. Pasando el relevo...")
            break

        pendientes = supabase.table("sitemap_urls").select("*").eq("procesado", False).order("id").limit(5).execute()

        if not pendientes.data:
            print("🏁 No hay más URLs pendientes. Proceso completo.")
            break

        for row in pendientes.data:
            procesar_url_pendiente(session, row)
            time.sleep(random.uniform(1.0, 2.5))  # Retraso orgánico entre URLs


if __name__ == "__main__":
    orquestador()
