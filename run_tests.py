"""Corre la búsqueda de tamaño mínimo para los idiomas de prueba.
Uso: python run_tests.py [id ...]"""
import json, sys
from multiprocessing import Pool
sys.path.insert(0, "src")
from pixelfont.search import find_min_size

import os
CJK = next((p for p in ("fonts_src/NotoSansCJK-Regular.ttc",
                        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc") if os.path.exists(p)),
           "fonts_src/NotoSansCJK-Regular.ttc")
CONFIGS = [
    dict(id="rumano-sans", name="Rumano · Noto Sans", font="fonts_src/NotoSans-VF.ttf", script="latin", ocr="ron",
         sizes=list(range(5, 15)),
         lines=["„Bună ziua!” — spuse învățătoarea.", "Știți că în țară, la Brașov, plouă?",
                "Mâine vom învăța împreună: ă, â, î, ș, ț."]),
    dict(id="rumano-serif", name="Rumano · Crimson Text (serif)", font="fonts_src/CrimsonText-Regular.ttf",
         script="latin", ocr="ron", sizes=list(range(6, 21)),
         lines=["„Bună ziua!” — spuse învățătoarea.", "Știți că în țară, la Brașov, plouă?",
                "Mâine vom învăța împreună: ă, â, î, ș, ț."]),
    dict(id="rumano-display", name="Rumano · Anton (condensada gruesa)", font="fonts_src/Anton-Regular.ttf",
         script="latin", ocr="ron", sizes=list(range(6, 21)),
         lines=["„Bună ziua!” — spuse învățătoarea.", "Știți că în țară, la Brașov, plouă?",
                "Mâine vom învăța împreună: ă, â, î, ș, ț."]),
    dict(id="japones", name="Japonés · Noto Sans CJK JP", font=CJK,
         script="japanese", ocr="jpn", sizes=list(range(8, 17)),
         lines=["日本語の文章を表示するテスト。", "東京の天気は晴れです。", "カタカナとひらがなと漢字を使います。"]),
    dict(id="tamil", name="Tamil · Noto Sans Tamil", font="fonts_src/NotoSansTamil-VF.ttf", script="tamil", ocr="tam",
         sizes=list(range(7, 19)),
         lines=["யாமறிந்த மொழிகளிலே தமிழ்மொழி போல் இனிதாவது எங்கும் காணோம்",
                "வணக்கம், நீங்கள் எப்படி இருக்கிறீர்கள்?"]),
    dict(id="urdu-nastaliq", name="Urdu · Noto Nastaliq Urdu", font="fonts_src/NotoNastaliqUrdu-VF.ttf", script="arabic",
         ocr="urd", sizes=list(range(10, 33, 2)),
         lines=["اردو ایک خوبصورت زبان ہے۔", "آپ کیسے ہیں؟", "پاکستان کا دارالحکومت اسلام آباد ہے۔"]),
    dict(id="urdu-naskh", name="Urdu · Noto Naskh Arabic", font="fonts_src/NotoNaskhArabic-VF.ttf", script="arabic",
         ocr="urd", sizes=list(range(7, 19)),
         lines=["اردو ایک خوبصورت زبان ہے۔", "آپ کیسے ہیں؟", "پاکستان کا دارالحکومت اسلام آباد ہے۔"]),
    dict(id="dzongkha", name="Dzongkha · Jomolhari", font="fonts_src/Jomolhari-Regular.ttf", script="tibetan", ocr="dzo",
         family="Dzong Pixel",  # "Jomolhari" es nombre reservado (OFL)
         sizes=list(range(8, 25)),
         lines=["བཀྲ་ཤིས་བདེ་ལེགས།", "འབྲུག་ཡུལ་གྱི་སྐད་ཡིག་ནི་རྫོང་ཁ་ཨིན།"]),
]


def run(cfg):
    return find_min_size(cfg, "salida", log=lambda m: print(m, flush=True))


if __name__ == "__main__":
    import os
    os.makedirs("salida", exist_ok=True)
    ids = sys.argv[1:]
    cfgs = [c for c in CONFIGS if not ids or c["id"] in ids]
    with Pool(2) as p:
        res = p.map(run, cfgs, chunksize=1)
    print(json.dumps({r["id"]: r["min_size"] for r in res}, ensure_ascii=False))
