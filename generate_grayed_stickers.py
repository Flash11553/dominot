"""
BİR DƏFƏLİK SKRİPT: Hər domino daşının "boz" (oynana bilməyən görünüşlü)
versiyasını STİKER olaraq yaradır (adi ağ daşlarla EYNİ tip/görünüş formatında).

NİYƏ LAZIMDIR:
Telegram Bot API mövcud bir stikerin görünüşünü canlı olaraq dəyişməyə
(boz/bulanıq etməyə) icazə vermir. Ona görə hər daşın YENİ, əlavə bir
"boz" STİKER versiyasını yaradıb Telegram-a göndəririk ki, yeni file_id
(və file_unique_id) alaq - main.py bunları hazır tapıb istifadə edəcək.

NECƏ İŞLƏYİR:
1. Hər daşın original stikerini Telegram-dan yükləyir
2. Pillow ilə boz-tonlu, solğun və bir az bulanıq versiyasına çevirir,
   Telegram stiker tələbinə uyğun 512px ölçüsünə salır
3. Yeni şəkli WEBP formatında STICKER olaraq göndərib yeni file_id/
   file_unique_id alır
4. Bütün nəticələri (hər ikisini) grayed_stickers.json faylına yazır

İŞLƏTMƏK ÜÇÜN:
1. Aşağıda STORAGE_CHAT_ID-ə öz Telegram istifadəçi ID-nizi yazın
   (botla əvvəlcə bir dəfə /start yazmış olmalısınız)
2. pip install -r requirements.txt (Pillow artıq var)
3. python3 generate_grayed_stickers.py
4. Nəticədə yaranan grayed_stickers.json-u main.py ilə EYNİ qovluqda saxlayın
5. Botu restart edin - hazırdır, əlavə kod dəyişikliyi lazım deyil
"""
import asyncio
import json
import io
import logging

from PIL import Image, ImageEnhance, ImageFilter
from aiogram.types import InputFile

from main import DOMINO, PASS_ID, TAKE_STONE_ID, bot

logging.basicConfig(level=logging.INFO)

# 🔴 BURAYA öz Telegram istifadəçi ID-nizi (rəqəm) yazın.
# Botla əvvəlcə bir dəfə /start yazmış olmalısınız ki, bu ID-yə mesaj göndərə bilsin.
STORAGE_CHAT_ID = 7578184117

STICKER_SIZE = 512  # Telegram stiker standartı - bir tərəf 512px olmalıdır


def make_grayed_sticker(image_bytes: bytes) -> bytes:
    """Şəkli boz-tonlu, solğun, yüngül bulanıq və stiker ölçüsündə WEBP-ə çevirir."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")

    # Boz-tonlu (grayscale) et, amma tam qara-ağ olmasın deyə orijinal rənglə qarışdır
    gray = img.convert("L").convert("RGBA")
    blended = Image.blend(img, gray, 0.75)

    # Kontrastı azalt (solğun görünsün)
    faded = ImageEnhance.Contrast(blended).enhance(0.5)

    # Yüngül bulanıqlıq (blur) əlavə et
    blurred = faded.filter(ImageFilter.GaussianBlur(radius=1.5))

    # Telegram stiker standartına uyğun ölçüləndir - ORİJİNAL PROPORSİYANI (en/boy
    # nisbətini) SAXLAYARAQ. DİQQƏT: kvadrat 512x512 kanvasa yapışdırmırıq!
    # Əvvəlki versiya şəkli süni şəkildə kvadrat çərçivəyə (şəffaf boşluqla)
    # yerləşdirirdi - bu, ağ (orijinal, kvadrat OLMAYAN) stikerlərlə fərqli
    # en/boy nisbəti yaradırdı və nəticədə Telegram-ın inline nəticə zolağında
    # ağ və boz daşlar arasında boşluqlar/uyğunsuzluq yaranırdı (skrinşotdakı
    # problem). İndi isə sadəcə uzun tərəfi 512px-ə endiririk, nisbət pozulmur -
    # boz stiker HƏMİŞƏ ağ stikerlə EYNİ en/boy nisbətinə malik olur.
    blurred.thumbnail((STICKER_SIZE, STICKER_SIZE), Image.LANCZOS)

    output = io.BytesIO()
    blurred.save(output, format="WEBP")
    return output.getvalue()


async def main():
    if STORAGE_CHAT_ID is None:
        print("❌ Zəhmət olmasa əvvəlcə bu faylın için STORAGE_CHAT_ID dəyişəninə öz Telegram ID-nizi yazın.")
        return

    all_stones = {**DOMINO, "PASS": PASS_ID, "TAKE": TAKE_STONE_ID}
    results = {}
    failed = []

    for stone, file_id in all_stones.items():
        try:
            file = await bot.get_file(file_id)
            downloaded = await bot.download_file(file.file_path)
            grayed_bytes = make_grayed_sticker(downloaded.read())

            sticker_file = InputFile(io.BytesIO(grayed_bytes), filename=f"{stone}.webp")
            sent = await bot.send_sticker(STORAGE_CHAT_ID, sticker=sticker_file)

            results[stone] = {
                "file_id": sent.sticker.file_id,
                "file_unique_id": sent.sticker.file_unique_id,
            }
            print(f"✅ {stone} -> hazırdır")
        except Exception as e:
            failed.append(stone)
            print(f"❌ {stone} -> XƏTA: {e}")

        await asyncio.sleep(0.3)  # rate-limit qorunması

    with open("grayed_stickers.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n🎉 Tamamlandı! {len(results)} daş üçün boz stiker yaradıldı, grayed_stickers.json-a yazıldı.")
    if failed:
        print(f"⚠️ {len(failed)} daş üçün alınmadı (çox güman animasiyalı/video stikerdirlər): {failed}")
        print("Bunlar üçün bot köhnə (normal, ağ) görünüşü göstərməyə davam edəcək - heç nə pozulmur.")


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
