# Mimari: arka planda ne oluyor?

Bu belge, bir fiş fotoğrafının sisteme girip `verified` / `flagged_for_review` damgasıyla veritabanına yazılmasına kadar geçen yolu anlatır.
## 1. Büyük resim

```
 Telefon / tarayıcı
        │  (fotoğraf)
        ▼
 Streamlit arayüzü ──HTTP──▶  FastAPI  (/api/v1)
  frontend/app.py                │
                                 ▼
        ┌──────────── POST /extract-receipt ────────────┐
        │ 1. preprocessor  OpenCV: kağıdı bul, düzelt   │
        │ 2. ocr_engine    PaddleOCR: yazıyı + kutuları  │
        │ 3. parser        alanları ayıkla               │
        │    (orientation  okunamadıysa 90/180° döndür)  │
        │ 4. reconciler    Matrah + Vergi == Toplam ?    │
        │ 5. categorizer   kategori tahmini              │
        │ 6. SQLite        kaydet (+ görseli diske)      │
        └────────────────────────────────────────────────┘
```

Her aşama `backend/app/services/` altında ayrı, tek sorumluluklu bir modüldür; `api/routes.py` bunları sırayla çağırır. Bağımlılıklar tek yönlüdür (parser yalnızca OCR çıktı tiplerini bilir, reconciler hiçbir şey import etmez), bu yüzden parser ve reconciler OCR modeli olmadan test edilir.

## 2. Aşamalar

### 2.1 Ön işleme — `services/preprocessor.py`
Amaç, OCR'a "masadaki kıvrık kağıt" yerine düz, kontrastlı bir görüntü vermek.
- Görüntü çözülür, çok büyükse küçültülür (en uzun kenar 3200 px).
- **Kağıdın dört köşesi** bulunur (kenar haritası + kontur + dörtgen uygunluk ve kenar desteği kontrolleri) ve **perspektif düzeltmesi** uygulanır. Kağıt bulunamazsa tam kare kullanılır ve uyarı eklenir.
- Zemin aydınlığı düzleştirilir, kontrast açılır (`enhanced`). Bulanıklık Laplacian varyansıyla ölçülür; eşik altıysa kullanıcıya uyarı gider.

### 2.2 OCR — `services/ocr_engine.py`
- **PaddleOCR (CPU)**, hafif "mobile" modeller. Tek `latin` modeli hem Türkçe hem İngilizce karakterleri okur.
- Motor **tek örnek (singleton)**: model bir kez yüklenir (`OCR_WARMUP=1` ile açılışta arka planda), istekler arasında paylaşılır. Paddle tahmincisi thread-safe olmadığı için çıkarım bir **kilit** arkasındadır; ağır iş `asyncio.to_thread` ile ayrı iş parçacığında koşar, böylece API donmaz.
- Çıktı: her metin satırı için metin, güven skoru ve 4 köşe koordinatı. `group_into_rows` bu kutuları **görsel satırlara** gruplar (aynı hizadaki `TOPLAM` ve `106,48` kutuları tek satır olur). Parser'ın "uzamsal" gücü buradan gelir.

### 2.3 Ayrıştırma — `services/parser.py`
Metinden alanları ayıklayan kural tabanlı katman (yapay zekâ yok, her karar açıklanabilir):
- **Tutar normalizasyonu (`parse_amount`)**: `1.250,50 TL`, `$1,250.50`, `₺106,48`, `*106,48`, `(12.50)`, `1O6,48` (harf O) gibi biçimleri `Decimal`'e çevirir. Tek ayraç + 3 hane = binlik ayracı (`1.250` = 1250).
- **Anahtar kelime satırları**: `TOPLAM`, `KDV`, `ARA TOPLAM`, `MATRAH`, `TOTAL`, `TAX`... Satırlar aksan/büyük-küçük harf duyarsız karşılaştırılır; `T0PLAM`, `TOPLAN` gibi küçük OCR hataları bulanık eşleşmeyle (difflib) düzeltilir. `NAKİT`, `PARA ÜSTÜ`, `İNDİRİM` satırları dışlanır.
- **Güçlü / zayıf anahtar**: `GENEL TOPLAM` > `TOPLAM`; `TOPKDV` > `KDV %10` satırlarının toplamı.
- **KDV oranları**: `KDV %10`, `EKMEK %01 *12,50` (Türk kasa fişlerinde oran ürün satırının sonundadır) ve OCR'ın `%` yerine okuduğu `$20` biçimleri okunur; yalnızca gerçek oranlar (1, 8, 10, 18, 20) kabul edilir, indirim/nakit satırları atlanır.
- **Matrah basılı değilse**: `KDV %10  9,68` satırından `9,68 × 100 / 10 = 96,80` türetilir (`TOPKDV` basılı olsa da; ikisi uyuşuyorsa). Bu değer *toplamdan bağımsızdır*, yani mutabakat hâlâ gerçek bir çapraz kontroldür (`toplam − vergi` ile türetseydik her fiş sahte yere "uyumlu" çıkardı). Oran satırı yoksa bkz. 2.5 (`implied_rate`).
- **Ödeme (bağımsız ikinci kanıt)**: `Nakit − Para üstü` (veya tek başına kart satırı) fişin gerçekte ne tahsil ettiğini söyler. Tek başına nakit satırı (para üstü olmadan) veya nakit+kart karışımı **kanıt sayılmaz** (bahşiş, parçalı ödeme olabilir).
- **Toplam onarımı**: Kasa fişlerinde tutarlar `*94,00` diye basılır ve OCR bazen `*` işaretini rakam okur (`*94.00` → `494.00`). Toplamın ilk hanesi atılmış aday (`94.00`) yalnızca **basılı değerin hiçbir kanıtı yokken** ve adayı en az bir bağımsız kanıt desteklerken (ödeme, `matrah + KDV`, KDV oranına uyum) kabul edilir; kullanıcıya uyarı verilir. Kanıt yoksa değer olduğu gibi kalır.
- **Bulanık anahtar kelime**: `TOPKOV` → `TOPKDV`, `TOPLAH` → `TOPLAM` gibi küçük OCR hataları onarılır.
- **Tarih**: `GG.AA.YYYY`, `YYYY-AA-GG`, 2 haneli yıl; imkânsız ve gelecek tarihler reddedilir; `TARİH` anahtar kelimeli satır öncelikli. `/` ayraçlı belirsiz tarihlerde USD'de ay-önce varsayılır ve uyarı verilir.
- **İşletme adı**: Üstteki satırlardan, **kutu yüksekliğine** göre (isim en büyük basılır); adres/telefon/tarih satırları elenir, `A.Ş.`, `LTD`, `MARKET` gibi sözcükler bonus alır.
- **Dil ve para birimi**: anahtar kelimeler + tutar biçimi (`1.250,50` TR / `1,250.50` EN) + `TL ₺ $ € £` işaretleri oy verir. Türkçe fişte çıplak `$` USD sayılmaz (OCR "Ş" harfini `$` okur: `$AHINTEPE`); yalnızca açık `USD` yazısı sayılır. Bu aynı zamanda `01/02/2024` gibi tarihlerin gün-önce okunmasını sağlar.

### 2.4 Yön düzeltme — `services/orientation.py`
Yan veya ters çekilmiş fişte toplam bulunamaz. Bu durumda (yalnızca o zaman) 90°/270°/180° döndürülmüş kopyalar okunur, en iyi okuma (toplam bulundu → bulunan alan sayısı → OCR güveni) seçilir ve kayıtlı görsel de çevrilir. Başarılı fişler için ek maliyet yoktur.

### 2.5 Mutabakat — `services/reconciler.py`
Projenin kalbi: `|Toplam − (Matrah + Vergi)| ≤ 0,02` ise **verified**, aksi hâlde **flagged_for_review**.
- Tüm hesap `Decimal` ile yapılır (float hatası olmaz: `0,10 + 0,20 == 0,30`).
- Tolerans 0,02 çünkü fişlerde KDV kalem/oran başına yuvarlanır; 1–2 kuruş fark normaldir.
- Karışık işaretler ve eksik veri işaretlenir; mesaj alanı nedenini yazar (`Mismatch: 100,00 + 20,00 = 120,00 ama toplam 130,00`).

**Doğrulama yöntemi (`method`).** Türk fişlerinin çoğunda matrah basılı değildir; yalnızca `TOPKDV` ve `TOPLAM` vardır. `Matrah = Toplam − KDV` demek mutabakatı totoloji yapar (her fiş uyumlu çıkar), bu yüzden dört yöntem ve her birinin kanıt gücü ayrı tutulur:

| `method` | Ne zaman | Kanıt |
|---|---|---|
| `printed` | Matrah basılıydı | Güçlü: kimlik doğrudan kontrol edildi |
| `derived_from_rate` | `KDV %10 9,68` satırından türetildi | Güçlü: matrah toplamdan bağımsız |
| `implied_rate` | Matrah ve oran satırı yok | Zayıf: `KDV / (Toplam − KDV)` oranı, fişte görülen oranlarla (veya yasal oranlarla) uyumlu mu? Tek oranda `KDV ≈ Toplam·r/(100+r)` (±0,10); karma oranlı fişte (ör. %1 ve %20 ürünler) bileşik oran iki oranın arasında olmalı |
| `manual` | Kullanıcı matrahı elle girdi | Kullanıcı onayı |
| `null` | Hiçbiri tutmadı | Doğrulanamadı → **flagged** |

**Çift mutabakat (ödeme kontrolü).** Vergi aritmetiğinden bağımsız ikinci bir kontrol: ödenen tutar (`nakit − para üstü` / kart) toplama eşit olmalı. Eşitse sonuç mesajına yazılır (`payment_matches: true`); çelişiyorsa fiş, KDV hesabı tutsa bile **flagged** olur, çünkü iki tutardan biri yanlış okunmuştur. Gerçek bir e-arşiv fişinde `*94.00` → `494.00` okunmuştu: ödeme (204 − 110 = 94) ve KDV oranı (15,67 ≈ 94·20/120) 94,00'ı destekliyordu, parser toplamı düzeltti ve bunu uyarıyla bildirdi.

`implied_rate` ile bulunan matrah (`Toplam − KDV`) veritabanına yazılır (CSV'de görünsün diye) ama **kanıt sayılmaz**: PATCH sonrası kimlik tekrar "totoloji" olmasın diye yöntem kayıtlıdır ve oran kontrolü yeniden çalışır. Arayüzdeki kart hangi yöntemle doğrulandığını yazar. Gerçek bir market fişiyle denendi: ürün satırlarında `%01`/`%20`, altta yalnızca `TOPKDV 46,60` ve `TOPLAM 427,42` → bileşik oran %12,2, `implied_rate` ile doğrulandı. Aynı fişte oran uyuşmazsa (ör. KDV 300) **flagged** kalır.

### 2.6 İşletme kimliği — `services/merchant.py`
Aynı işletme OCR'da her seferinde biraz farklı okunur (`ORNEK MARKET A.S` / `Örnek Market A.Ş.`). Vergi numarası her fişte basılı olmadığı için kimlik yalnızca addan üretilir: aksansız büyük harf, noktalama atılır, şirket tipi sözcükler (`A.Ş.`, `LTD`, `ŞTİ`, `TİCARET`, `SANAYİ`...) çıkarılır → **anahtar** (`MIGROS TICARET A.S.` → `MIGROS`). Yeni anahtar, bilinen bir anahtara %90+ benziyorsa (tek yanlış harf, en az 6 karakter) ona yapıştırılır; kısa adlar (`BIM`, `A101`) birebir eşleşmelidir. Otomatik eşleşme kaçırırsa kullanıcı iki işletmeyi elle birleştirir (`POST /merchants/merge`). Grafikler ve filtreler bu anahtarla gruplanır.

### 2.7 Kategori — `services/categorizer.py` + öğrenme
Anahtar kelime puanlama: işletme adındaki eşleşme 3, fiş metnindeki 1 puan (Türkçe ek toleranslı: `ECZANE` → `ECZANESI`). Model yok, ağ yok, tamamen açıklanabilir.

**Düzeltmeden öğrenme**: kullanıcı bir fişin kategorisini değiştirince `merchant_rules` tablosuna (işletme anahtarı → kategori) yazılır. O işletmenin sonraki fişleri otomatik bu kategoriyle gelir; aynı işletmenin kategorisi hâlâ boş/`other` olan eski fişleri de ona uyar, kullanıcının bilinçli seçtiği farklı kategoriler değiştirilmez.

## 3. Veri katmanı — `app/db/`
- **SQLite + SQLAlchemy 2.0 async (aiosqlite)**. WAL kipi ve `busy_timeout` açık: okuyucular yazıcıyı bekletmez.
- Tek tablo `expenses`: işletme, tarih, matrah, vergi, toplam, para birimi, kategori, mutabakat durumu, ham OCR metni, görsel yolu, oluşturulma zamanı.
- Tutarlar `Numeric(12,2)`; Python tarafında `Decimal`, JSON'da sayı. Pydantic `Money` tipi 2 haneye yarım-yukarı yuvarlar.
- Tablolar: `expenses` ve `merchant_rules` (işletme anahtarı → öğrenilmiş kategori). `expenses` ayrıca `merchant_key` (işletme kimliği) ve `reconciliation_method` (bkz. 2.5) taşır.
- **Hafif şema göçü**: `create_all` var olan tabloyu değiştirmez; `init_db` eksik nullable kolonları (`category`, `merchant_key`, `reconciliation_method` gibi) `ALTER TABLE` ile ekler ve eski satırların `merchant_key` değerini doldurur. Eski bir `expense.db` veya Docker volume'ü açılışta kendiliğinden yükseltilir (test edildi). Büyürse yerini Alembic alır.
- Görseller `UPLOAD_DIR` altında rastgele UUID adıyla saklanır (istemcinin dosya adı hiç kullanılmaz); servis ederken yolun `UPLOAD_DIR` içinde olduğu doğrulanır (path traversal koruması).

## 4. API — `api/routes.py` (`/api/v1`)

| Uç nokta | Ne yapar |
|---|---|
| `POST /extract-receipt` | Tüm hattı çalıştırır, kaydeder; kutular, güven ve uyarılarla döner |
| `GET /expenses` | Filtre (işletme, para birimi, durum, kategori, tarih) + sayfalama |
| `GET /expenses/export.csv` | Aynı filtrelerle CSV (`locale=tr`: `;` + virgül, Excel TR için; `en`: `,` + nokta) |
| `GET /stats` | Para birimi / ay / kategori / işletme toplamları (SQL'de hesaplanır, işletmeler anahtara göre gruplanır) |
| `GET /merchants` | İşletmeler (yazılış varyantları birleşik), fiş sayısı, öğrenilmiş kategori |
| `POST /merchants/merge` | İki işletme anahtarını elle birleştir |
| `GET /expenses/{id}` | Tek fiş + mutabakat açıklaması (neden "şüpheli"); kaydetmeden yeniden hesaplanır |
| `GET /expenses/{id}/image` | Düzeltilmiş fiş görseli |
| `PATCH /expenses/{id}` | Değerleri düzelt → durum yeniden hesaplanır |
| `DELETE /expenses/{id}` | Kaydı ve görseli sil |
| `GET /health` | Canlılık + veritabanı (Docker healthcheck bunu kullanır) |

Etkileşimli doküman: `http://localhost:8000/docs`.

**Mesaj dili.** Kullanıcıya görünen metinler (uyarılar, hata `detail` alanları, mutabakat `message`) Türkçe, kısa ve eyleme yönelik yazılır ("Fotoğraf bulanık görünüyor. Telefonu sabit tutup ışığı artırarak tekrar çekmeniz önerilir."). Programların kullandığı alanlar (`status`, `method`, `payment_matches`) sabit İngilizce kodlardır; arayüz bunlara bağlı kalır, metni ayrıştırmaz.

Güvenlik/sağlamlık notları: yükleme boyutu sınırı (20 MiB), boş/bozuk görsel için anlamlı 4xx, bilinmeyen sorgu parametresi 422, `LIKE` joker karakterleri kaçırılır, CSV'de `= + - @` ile başlayan hücreler formül enjeksiyonuna karşı etkisizleştirilir, OCR hazır değilse 503.

## 5. Arayüz — `frontend/app.py`, `ui.py`, `styles.css`
Streamlit; yalnızca API ile konuşur (OCR çalıştırmaz). Üç dosya:

- `app.py`: akış ve API istemcisi. Üst satırda **Fiş ekle** (kamera / dosya / örnek fiş) ve **sonuç kartı**; fiş okununca ikinci satırda kutuları güven rengine göre çizilmiş görsel ile **düzeltme formu**; altta **harcama özeti** (KPI kartları, kategori/ay/işletme grafikleri, filtreli son fişler tablosu, CSV indirme, işletme birleştirme, silme).
- `ui.py`: HTML bileşenleri (veri → kaçışlı tek satır HTML). Saf fonksiyonlardır, testlidir (kaçışlama, Markdown güvenliği, erişilebilir tablo/grafik yapısı).
- `styles.css`: tasarım sistemi (açık/koyu palet değişkenleri, tipografi, düğme/giriş/uyarı stilleri, mobil düzen). Gerekçeler ve kaynaklar [`DESIGN.md`](DESIGN.md)'de.

Grafikler ve tablo Streamlit'in yerleşik bileşenleri yerine HTML/CSS ile çizilir: açık/koyu temaya tam uyum, her değerin yazılı olması, sabit genişlikli rakamlar ve erişilebilir yapı için. Çizim animasyonu yoktur (işlevsel veri yeniden animasyon almaz). Telefonda tablo satırları küçük kartlara dönüşür, yatay taşma olmaz.

### 5.1 Mobil PWA — `backend/app/static/m/`
Telefon için ayrı, ince bir arayüz; FastAPI `/m/` altında statik olarak sunar (aynı kaynak: CORS yok, ek sunucu yok). Düz HTML/CSS/JS, derleme adımı ve bağımlılık yok. Yalnızca yakala-doğrula-düzelt:

- **Çekim ve yükleme:** `<input type=file capture=environment>` kamera uygulamasını açar (HTTPS gerekmez). Fotoğraf istemcide küçültülür (en uzun kenar 2400 px, JPEG 0,85; sunucu zaten 3200/2400 px'e indirir) ve **tek tek, sırayla** yüklenir: sunucuda tek OCR motoru var, art arda istek yarar sağlamaz; kullanıcı okuma sürerken yenisini çekebilir.
- **Kanıtlı sonuç:** "✓ Mutabık" ya da "⚠ Şüpheli" + nedeni (`GET /expenses/{id}` → `reconciliation.message`). İnceleme listesi sunucudaki `flagged_for_review` kayıtlarıdır; sekme kapansa da kaybolmaz.
- **Düzeltme:** yalnızca değişen alanlar `PATCH`'lenir (değişmeyen matrahı göndermek onu "elle girildi" saydırırdı); tutar ayrıştırıcısı `1.234,50` biçimini kabul eder, anlaşılmazı (`12,3,4`) reddeder.
- **Kötü çekim uyarısı:** istemcide gri tonlamada Laplace varyansı (bulanıklık) ve parlaklık yüzdeliği. Yalnızca öneridir, yüklemeyi engellemez; eşikler sentetik bozulmayla ayarlandı, gerçek telefon fotoğraflarıyla ayarlanmadı.
- **PWA kabuğu:** manifest + service worker (kabuk önbelleği). Service worker ve "uygulamayı yükle" yalnızca güvenli bağlamda (HTTPS ya da localhost) çalışır; bu proje için Tailscale ile HTTPS kullanıldı (README).
- Sunucudan gelen her metin `textContent` ile yazılır (`innerHTML` yok).

## 6. Dağıtım
`docker compose up --build`: çok aşamalı backend imajı (derleyici son imajda yok, OCR modelleri imaja gömülü → ilk istek internetsiz çalışır, kök olmayan kullanıcı, healthcheck), ayrı frontend imajı, `expense_data` volume'ü veritabanını ve görselleri saklar.

## 7. Test ve ölçüm
- **Gerçek hata fişleri testi** (`tests/test_real_receipts.py`): gerçek bir fiş yanlış okunduğunda OCR satırları *önce* bu dosyaya eklenir, test kırılırken görülür, sonra kod düzeltilir. Fişlerdeki kişisel/terminal bilgileri çıkarılır.
- `backend/tests` (pytest): reconciler (yöntemler ve ödeme kontrolü dahil), parser (tutar/tarih/anahtar kelime/OCR hataları/gerçek fiş düzeni), kategori, işletme anahtarı, yön düzeltme, veritabanı göçü ve **tüm API** (gerçek SQLite + gerçek ön işleme, OCR yerine sahte motor → hızlı, model gerektirmez).
- `backend/eval`: tüm hattın alan bazlı doğruluğunu ölçer; sentetik bozulmalarla (döndürme, bulanıklık, gürültü, loş ışık, eğiklik) dayanıklılığı gösterir. **Gerçek fişlerinizle** `python -m eval.evaluate --dir klasörünüz` çalıştırıp gerçek sayıyı README'ye yazın.
- GitHub Actions: her push'ta testler.

## 8. Sınırlar
- Doğruluk az sayıda gerçek fişle denendi, kapsamlı ölçülmedi; örnek fişler kurgusaldır.
- Kalem (satır) bazlı ürün çıkarımı yok; yalnızca toplamlar.
- `lower()` SQLite'ta yalnızca ASCII: Türkçe `İ/ı` ile işletme aramasında büyük/küçük harf kaçabilir.
- İşletme birleştirme addan yapılır: iki harften fazla bozuk okunmuş ad otomatik birleşmez (elle birleştirilir). Farklı işletmelerin benzer adları (nadiren) yanlışlıkla birleşebilir; eşik bilerek yüksek (%90).
- Toplam onarımı yalnızca baştaki **tek bir fazla hane** için çalışır (`*` → rakam). Ödeme satırı da bozuksa veya fişte hiç bağımsız kanıt yoksa yanlış okunan toplam düzeltilemez; fiş incelemeye düşer ve kullanıcı düzeltir.
- `implied_rate` zayıf kanıttır: oranları 1 ile 20 arasında kalan her makul fiş geçer. Bu yüzden arayüzde ayrı etiketle gösterilir.
- `/stats` ay gruplaması SQLite'a özgü `strftime` kullanır (PostgreSQL'de `to_char`).
- Kimlik doğrulama yok (tek kullanıcılı demo).
