# Tasarım sistemi

Arayüz muhasebe uygulamasına yakışacak şekilde sakin, okunaklı ve güven veren olmalı: **derin lacivert-mavi** marka rengi, soğuk gri yüzeyler, yalnızca anlam taşıyan durum renkleri (yeşil: mutabık, kehribar: incelenecek, kırmızı: hata) ve sayılar için sabit genişlikli rakamlar. Açık ve koyu görünüm **sistemi izler** (uygulamaya özel anahtar yok).

Kaynaklar (okunup uygulanmıştır; metin kopyalanmamış, ilkeler özetlenmiştir):

- Apple Human Interface Guidelines: [Color](https://developer.apple.com/design/human-interface-guidelines/color), [Dark Mode](https://developer.apple.com/design/human-interface-guidelines/dark-mode), [Typography](https://developer.apple.com/design/human-interface-guidelines/typography), [Layout](https://developer.apple.com/design/human-interface-guidelines/layout), [Accessibility](https://developer.apple.com/design/human-interface-guidelines/accessibility), [Motion](https://developer.apple.com/design/human-interface-guidelines/motion), [Buttons](https://developer.apple.com/design/human-interface-guidelines/buttons), [Text fields](https://developer.apple.com/design/human-interface-guidelines/text-fields), [Segmented controls](https://developer.apple.com/design/human-interface-guidelines/segmented-controls), [Charting data](https://developer.apple.com/design/human-interface-guidelines/charting-data), [Alerts](https://developer.apple.com/design/human-interface-guidelines/alerts), [Writing](https://developer.apple.com/design/human-interface-guidelines/writing).
- Emil Kowalski, [skills](https://github.com/emilkowalski/skills) (`emil-design-eng`, `animate`, `review-animations`, `remove-animations`, `mobile-native`, `break-ui`, cheatsheet'ler).

> HIG'nin pt değerleri Apple platformlarına özgüdür; web için boyutlar ve aralıklar bu sistemin kendi ızgarasıdır. HIG'de tablo sayfası bulunamadı (404), tablo kararları bu belgedeki ilkelerden türetilmiştir.

## Kaynaktan karara

| İlke (kaynak) | Bu projede |
|---|---|
| Anlamsal renk; aynı renk iki farklı anlam taşımaz (HIG Color) | Marka mavisi yalnızca etkileşim (düğme, bağlantı, odak, grafik); yeşil yalnızca *mutabık*; kehribar yalnızca *incelenecek*; kırmızı yalnızca hata/silme |
| Renk tek başına anlam taşımaz (HIG Color, Accessibility) | Durum her yerde **simge + sözcük + renk**: ✓ Mutabık, ⚠ İncelenecek. Grafiklerde her çubuğun değeri yazılı |
| Kontrast: metin ≥ 4,5:1, küçük özel metinde 7:1 hedefi (HIG Dark Mode, Accessibility) | Aşağıdaki tablo, her renk çifti `WCAG` formülüyle doğrulandı; birincil metin 17,9:1 (açık) / 14,9:1 (koyu) |
| Açık ve koyu görünüm; uygulamaya özel görünüm anahtarı yok; koyu modda dim zemin, parlak ön plan, yükseltilmiş yüzey daha açık; beyaz içerik görseli parlamasın (HIG Dark Mode) | `prefers-color-scheme` ile iki palet; kartlar zeminden daha açık; fiş fotoğrafına koyu modda `brightness(.9)` |
| `prefers-contrast: more` (HIG Accessibility, Emil) | Kenarlar ve ikincil metin belirginleşir |
| Hafif ağırlık yok; hiyerarşi ağırlık, boyut ve renkle (HIG Typography) | 500–700 ağırlık; başlık 20–28 px / 700, kart başlığı 17 px / 600, gövde 15 px, ikincil 13–14 px, en küçük 11 px |
| Sabit genişlikli rakamlar, tutar/tarih asla kısaltılmaz (Emil: `tabular-nums`, break-ui) | Tüm tutarlar `font-variant-numeric: tabular-nums`, sağa hizalı; uzun işletme adı kısaltılır, tutar **asla** |
| Büyük başlıklarda `-0.02em` iz aralığı (Emil) | 20 px üstü başlıklar |
| Dokunma hedefi 44 pt, hedefler arası boşluk (HIG Accessibility) | Dokunmatik cihazda düğme/giriş ≥ 44 px, kontrol arası 12–16 px |
| iOS'ta yakınlaştırmayı tetiklemeyen 16 px giriş; `touch-action: manipulation`; `-webkit-tap-highlight-color` (Emil mobile-native) | Telefonda giriş alanları 16 px |
| Görünümde en fazla bir-iki birincil düğme; yıkıcı eylem birincil rol almaz; geri alınamaz eylem onay ister (HIG Buttons, Alerts) | Birincil: *Fişi oku*, *Kaydet ve yeniden doğrula*; *Kalıcı olarak sil* kırmızı çerçeveli ikincil ve popover içinde "geri alınamaz" notuyla |
| Düğme etiketi fiille başlar; sade, kısa dil; hata suçlamaz ve ne yapılacağını söyler; boş durum sonraki adımı söyler (HIG Writing) | "Fişi oku", "CSV indir"; "Henüz fiş işlenmedi — soldan bir fiş ekleyin" |
| Segmentli kontrol: eşit genişlik, isim etiketleri, 5'ten az seçenek, emoji karışmaz (HIG Segmented controls) | Kamera / Dosya / Örnek fiş; para birimi seçici |
| Grafik: basit çubuk, başlık + özet cümle, değer etiketi, tutarlı stil (HIG Charting data) | Üç sade çubuk grafiği; alt başlıkta özet ("en büyük: Market (%64)") |
| Kontroller içerikten ayrı; gruplama boşluk ve kapsayıcıyla (HIG Layout) | Kart = grup; araç çubuğu tablonun üstünde |
| Sık kullanılan kontrolde hareket yok; işlevsel veri (grafik, tablo, sayı) yeniden animasyon almaz (Emil) | Grafik/tablo/KPI'da **hiç animasyon yok**; sayılar saymaz, çubuklar büyümez |
| Basınca `scale(.97)`, 160 ms `ease-out`; hover yalnızca `(hover: hover) and (pointer: fine)`; `transition: all` yok; süre < 300 ms (Emil) | Tüm düğmelerde; yalnızca `transform`, `background-color`, `border-color`, `color` geçişli |
| Güçlü eğriler `cubic-bezier(.23, 1, .32, 1)`; asla `ease-in` (Emil) | `--fm-ease-out` |
| `prefers-reduced-motion`: dönüşüm hareketi kalkar, renk geçişi kalır (Emil, HIG Motion) | Basma ölçeklemesi kapanır |
| Katmanlı, yarı saydam gölge; ince halka + yumuşak derinlik (Emil) | `--fm-shadow-card`, `--fm-shadow-pop` |
| Gerçekçi/kötü senaryo verisiyle dene: çok uzun ad, 0/1 sayısı, negatif tutar, 320 px, koyu mod (Emil break-ui) | Gerçek bozuk OCR adı ("MEHMETEFENDI TURK KA ERNET LAVABO…") 2 satırda kırpılır, tutar yerinde kalır; 375 px'te yatay taşma yok |

## Renk (WCAG kontrast doğrulaması)

Tüm çiftler `frontend/tests/test_palette.py` ile **her çalıştırmada** doğrulanır (metin ≥ 4,5:1; birincil metin ≥ 7:1; kontrol kenarı ve dolgu ≥ 3:1). Test paleti `styles.css`'ten okur, yani bir rengi değiştirmek kontrastı bozarsa test kırılır. Ayrıca koyu paletin açık paletteki her rengi geçersiz kıldığını ve koyu yüzeylerin zeminden daha açık olduğunu denetler.

| Rol | Açık | Koyu |
|---|---|---|
| Zemin (canvas) | `#F3F5F9` | `#0A101C` |
| Yüzey (kart) | `#FFFFFF` | `#121A2B` (daha açık: yükseltilmiş) |
| Metin | `#0E1726` (17,9:1) | `#E9EEF8` (14,9:1) |
| Metin 2 | `#46536A` (7,8:1) | `#AEB9CE` (8,8:1) |
| Metin 3 | `#5B687D` (5,6:1) | `#8F9CB5` (6,3:1) |
| Kontrol kenarı | `#7C8AA0` (3,5:1) | `#66759A` (3,8:1) |
| Marka | `#1F4FD8` (6,6:1), düğme `#1F4FD8` | metin `#86A9FF` (7,6:1), düğme `#2F5BE0` (5,7:1, beyaz yazı) |
| Mutabık | metin `#12663A`, zemin `#E6F5EC` | metin `#6FE0A5`, zemin `#10301F` |
| İncelenecek | metin `#8A4B08`, zemin `#FDF1DC` | metin `#F6BE55`, zemin `#33250A` |
| Hata / silme | metin `#B3261E`, zemin `#FCEAE8` | metin `#FF9890`, zemin `#3B1614` |

## Tipografi

Sistem yazı tipi yığını: `-apple-system, "SF Pro Text", "Segoe UI Variable", "Segoe UI", Inter, Roboto, …` (Apple cihazlarında SF, Windows'ta Segoe UI; harici yazı tipi indirilmez).

| Kullanım | Boyut / satır | Ağırlık |
|---|---|---|
| Marka, bölüm başlığı | 20 / 24–25 | 700, −0,02em |
| Sonuç başlığı | 22 / 28 | 700, −0,02em |
| KPI değeri | 26 / 32 (telefonda 22 / 28) | 700, tabular |
| Kart başlığı | 17 / 22 | 600 |
| Gövde | 15 / 22 | 400–500 |
| İkincil metin, etiket | 13–14 / 18–20 | 500–600 |
| En küçük (sütun alt etiketi) | 11 / 15 | 500 |

## Boşluk ve şekil

8 pt ızgarası (4, 8, 12, 16, 20, 24, 32). Kart yarıçapı 16, iç yüzey 12, kontrol 10, hap 999. Sayfa içeriği en fazla 1200 px, telefonda 16 px kenar boşluğu. Kartlar kenarlık yerine ince halka + yumuşak gölge kullanır.

## Hareket

Yalnızca geri bildirim: düğmede basma (`scale(.97)`, 160 ms) ve renk geçişi (150 ms). Giriş animasyonu, sayaç, grafik çizimi yok. Streamlit her etkileşimde bileşenleri yeniden çizdiği için sık yeniden oynayacak hareket zaten bu projede doğru seçim değildir (Emil: sık kullanılan yüzeyde hareketi sil).

## Uygulama notları (Streamlit 1.41.1)

- Streamlit, yerel temasını `prefers-color-scheme`'e göre seçer; `config.toml`'da `[theme]` tanımlamak sistem takibini kapatacağı için **tanımlanmadı**. Renkler `frontend/styles.css` içindeki değişkenlerle, aynı medya sorgusuyla verilir.
- Bileşenler `frontend/ui.py` içinde saf fonksiyonlardır (veri → kaçışlı tek satır HTML) ve testlidir. `<p>` kullanılmaz (Streamlit her markdown `<p>`'sini biçimlendirir), `$` kaçışlanır (Markdown formül sanılmasın), dinamik her metin `html.escape` ile geçer.
- Açılır menü, popover ve takvim `body` seviyesinde bir portalda çizilir; bu yüzden seçiciler `.stApp` yerine `:root` ile başlar.
- `st.html`, `<svg>` öğelerini siler; simgeler `st.markdown(..., unsafe_allow_html=True)` ile satır içi SVG olarak verilir. Stil bloğunda ham `<` karakteri bulunmaz (HTML temizleyicisi o stili siler).
- Streamlit'in İngilizce yükleme/kamera metinleri CSS (`::after`) ile Türkçeleştirildi. Takvimdeki ay/gün adları ve kamera izin metni Streamlit tarafından İngilizce üretilir; CSS ile güvenle değiştirilemedi.

## Bilinen sınırlar

- Kamera erişimi yalnızca HTTPS/localhost'ta çalışır; kamera tarafı tarayıcı panelinde denenemedi.
- Takvim ay/gün adları ve kamera izni metni İngilizce kalır (Streamlit).
- Aylık grafik 12 aydan fazla veride yatay kayar; tablo ve kartlar 320 px'e kadar denendi.
