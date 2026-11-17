# EV Charge Controller / EV Şarj Kontrolcüsü

Temporary Cloud API panel for Tuya/SmartLife EV Charging Station (Bluetooth-only device).  
Future: Local control via ESP32-S3 + ESPHome Bluetooth Proxy.

---

## 🎯 Özellikler / Features

| Türkçe / Turkish | English |
|------------------|---------|
| **Cloud API polling** — Tuya IoT üzerinden DP okuma/yazma | **Cloud API polling** — Read/write DPs via Tuya IoT |
| **Bluetooth-only cihaz** — Yerel WiFi yok, ESP32 proxy planlanıyor | **Bluetooth-only device** — No local WiFi, ESP32 proxy planned |
| **DP kontrol** — Akım (6-32A), çalışma modu (now/pct/energy/schedule), başlat/durdur | **DP control** — Current (6-32A), work mode (now/pct/energy/schedule), start/stop |
| **Şarj geçmişi** — Session bazlı, günlük kWh bar chart (1h/6h/24h/7d/30d) | **Charge history** — Session-based, daily kWh bar chart |
| **30 sn collector** — APScheduler ile oturum başlangıç/bitiş tespiti | **30s collector** — APScheduler session start/end detection |
| **Glassmorphism UI** — Slider, dropdown, toggle, toast notifications | **Glassmorphism UI** — Slider, dropdown, toggle, toast notifications |
| **Tailscale erişim** — `http://100.125.108.13:8903/` | **Tailscale access** — `http://100.125.108.13:8903/` |
| **systemd servisi** — `tuya-ev.service`, `enable --now`, `Restart=always` | **Systemd service** — `tuya-ev.service`, `enable --now`, `Restart=always` |

---

## 📦 Kurulum / Installation

### 1. Bağımlılıklar / Dependencies

```bash
sudo apt update && sudo apt install python3-venv sqlite3
cd /home/tekinaltun/projects/EV_Charge_Controller
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Tuya Cloud Credentials (bir kez / one-time)

- https://iot.tuya.com → Cloud → Project oluştur (Smart Home, Custom)
- **Access ID** / **Access Secret** kopyala
- Devices → Link Tuya App Account → SmartLife hesabını QR ile bağla
- Service API: IoT Core, Authorization, Device Status Notification etkinleştir

### 3. Wizard ile Local Key Alma (EV şarj cihazı için)

```bash
cd /home/tekinaltun/projects/EV_Charge_Controller
./venv/bin/python -m tinytuya wizard
# Access ID, Access Secret, Region (eu) gir
# SmartLife App UID istenirse gir
# devices.json → "EV Şarj Noktası" local_key ve IP al
```

### 4. Cihaz Bilgileri (app_ev.py içinde güncelle)

```python
DEVICE_ID = "bfd515c92ae48e2725jxzg"    # EV Şarj Noktası Device ID
LOCAL_KEY = "*tY}#&yIU2EHTW5#"          # Wizard'dan alınan Local Key
DEVICE_IP = "192.168.1.xxx"             # Yerel IP (eğer LAN'da görünüyorsa)
DEVICE_VERSION = 3.3                    # veya 3.5 (cihaza göre)
```

> **Not:** Bu cihaz **Bluetooth-only**, WiFi broadcast yapmayabilir. `tinytuya scan` ile bulunmazsa Cloud API modunda çalışır (şu anki durum).

### 5. Systemd Servisi / Systemd Service

```bash
sudo cp tuya-ev.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tuya-ev.service
sudo systemctl status tuya-ev.service
```

Loglar / Logs:
```bash
journalctl -u tuya-ev.service -f
```

---

## 🌐 Erişim / Access

| Ortam / Environment | URL |
|---------------------|-----|
| Yerel ağ / Local LAN | `http://<makine-ip>:8903/` |
| Tailscale | `http://100.125.108.13:8903/` |

---

## 📊 DP Şeması / DP Schema (Tuya Cloud `getfunctions` sonucu)

| DP | Açıklama / Description | Tip / Type | Değerler / Values | Yazılabilir / Writable |
|----|------------------------|------------|-------------------|------------------------|
| 3 | Çalışma Durumu / Work Status | Enum | `charger_charging`, `charger_idle`, `charger_waiting` | ❌ |
| 4 | Akım Limiti / Current Limit | Integer | 6-32A (slider step 2) | ✅ |
| 9 | Anlık Güç / Instant Power | Integer | Watt × 1000 | ❌ |
| 14 | Çalışma Modu / Work Mode | Enum | `charge_now`, `charge_pct`, `charge_energy`, `charge_schedule` | ✅ |
| 18 | Şarj Aktif / Charging Active | Bool | true/false | ❌ |
| 25 | Toplam Enerji / Total Energy | Integer | kWh × 100 | ❌ |

**Slider not:** DP4 (akım) UI'da 6-32A aralığı, step 2.

---

## 🗄️ Geçmiş Veritabanı / History Database

- **Dosya / File:** `ev_history.db` (SQLite)
- **Tablo / Table:** `sessions(ts_start, ts_end, energy_wh, duration_s, max_power_w, dp14_mode)`
- **Toplayıcı / Collector:** APScheduler, her 30 sn (DP3, DP18, DP25 ile session detection)
- **API:** `GET /history?hours=168` → günlük kWh array döner

---

## 🔮 Gelecek / Future (ESP32-S3 Local Control)

| Bileşen / Component | Durum / Status |
|---------------------|----------------|
| ESP32-S3-DevKitC-1 N16R8 (2 adet satın alındı) | ✅ Elimizde |
| ESPHome Bluetooth Proxy firmware | ⏳ Planlandı |
| Tuya BLE servis/characteristic keşfi | ⏳ Yapılacak |
| Yerel DP kontrol (tinytuya/ESPHome) | ⏳ Cloud yerine geçecek |
| MQTT + Home Assistant entegrasyonu | ⏳ Planlandı |

---

## 📁 Dosya Yapısı / File Structure

```
EV_Charge_Controller/
├── app_ev.py               # Ana Flask uygulaması (port 8903)
├── requirements.txt        # Python bağımlılıkları
├── tuya-ev.service         # systemd servis dosyası
├── ev_history.db           # SQLite session veritabanı (otomatik oluşur)
└── README.md               # Bu dosya
```

---

## 🔧 Sorun Giderme / Troubleshooting

| Sorun / Issue | Çözüm / Solution |
|---------------|------------------|
| Cihaz "Offline" | Bluetooth-only → ESP32 proxy gelene kadar Cloud API kullanılır / Use Cloud until ESP32 proxy |
| Slider çalışmıyor | DP4 yazma Cloud'tan mı yerelden mi? Debounce 450ms + reload 6s kontrol et / Check debounce |
| Geçmiş boş | `ev_history.db` dolu mu? Collector çalışıyor mu (loglarda `APScheduler` var mı?) |
| Akım sınırı 32A'dan fazla gelmiyor | UI slider max=32A, cihaz donanım sınırı da 32A olabilir / Hardware limit |

---

## 🔗 İlgili Projeler / Related Projects

- **Isı Pompası Yöneticisi:** `Tuya_Head_Pomp_manager` (port 8902) — Yerel tinytuya
- **BYD Araç Paneli:** `BYD_Remote_APP` (port 8904) — `pybyd` Cloud API
- **ESP32-S3 Gateway:** BLE proxy + Zigbee coordinator + Matter border router

---

## 📝 Lisans / License

MIT — Kişisel kullanım için özgür / Free for personal use.