#!/usr/bin/env bash
# Descarga las fuentes de prueba (todas OFL) desde el repo de Google Fonts.
set -e
tmp=$(mktemp -d)
git clone --depth 1 --filter=blob:none --sparse https://github.com/google/fonts "$tmp/gf"
git -C "$tmp/gf" sparse-checkout set --no-cone ofl/notosans ofl/notosanstamil ofl/notonastaliqurdu \
  ofl/notonaskharabic ofl/jomolhari ofl/crimsontext ofl/anton
mkdir -p fonts_src
cp "$tmp/gf/ofl/notosans/NotoSans[wdth,wght].ttf" fonts_src/NotoSans-VF.ttf
cp "$tmp/gf/ofl/notosanstamil/NotoSansTamil[wdth,wght].ttf" fonts_src/NotoSansTamil-VF.ttf
cp "$tmp/gf/ofl/notonastaliqurdu/NotoNastaliqUrdu[wght].ttf" fonts_src/NotoNastaliqUrdu-VF.ttf
cp "$tmp/gf/ofl/notonaskharabic/NotoNaskhArabic[wght].ttf" fonts_src/NotoNaskhArabic-VF.ttf
cp "$tmp/gf/ofl/jomolhari/Jomolhari-Regular.ttf" fonts_src/
cp "$tmp/gf/ofl/crimsontext/CrimsonText-Regular.ttf" fonts_src/
cp "$tmp/gf/ofl/anton/Anton-Regular.ttf" fonts_src/
echo "Japonés: instala Noto Sans CJK JP (p. ej. paquete fonts-noto-cjk) o pon NotoSansCJK-Regular.ttc en fonts_src/"
echo "OCR: Tesseract 5 + traineddata ron, jpn, tam, urd, dzo (github.com/tesseract-ocr/tessdata_fast); PIXELFONT_TESSDATA=<carpeta>"
