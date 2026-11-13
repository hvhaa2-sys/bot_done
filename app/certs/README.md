# Сертификат MAX

`russian_trusted_root_ca.pem` — публичный корневой сертификат Минцифры, не секретный ключ.
Скачан 13.11.2026 по HTTPS с проверкой сертификата источника:
https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt

Источник ссылки: https://developers.sber.ru/docs/ru/gigachat/certificates
Требование MAX: https://dev.max.ru/docs-api

SHA-256 отпечаток DER:
`D26D2D0231B7C39F92CC738512BA54103519E4405D68B5BD703E9788CA8ECF31`.
Срок действия: 01.03.2022–27.02.2032.

`app/tls.py` добавляет его только в SSL-контекст официальных адресов MAX API.
Системное хранилище и пакет certifi не меняются. Проверка цепочки и имени сервера остаётся включена.
