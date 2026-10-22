# PumpChicken ($Chicken) — Whitepaper

**Wersja 1.0 · Robinhood Chain (chainId 4663)**

> Dokument informacyjny, nie jest poradą inwestycyjną. Kryptoaktywa są ryzykowne i można stracić cały kapitał. Kontrakt nie został jeszcze zaudytowany przez niezależną firmę — audyt jest zalecany przed wdrożeniem na mainnet.

---

## 1. Wizja

PumpChicken to token **Chicken** oraz „kontrakt-matka" (launchpad). Matka ma własny wbudowany DEX ETH/token i pozwala tworzyć kolejne meme tokeny, które rodzą się razem z pulą płynności. Wszystko działa na Robinhood Chain.

Cele:
- uczciwy, przejrzysty start tokena, z jawnymi zasadami zapisanymi w kodzie,
- jeden kontrakt, który obsługuje handel, płynność i tworzenie nowych tokenów,
- ochrona kupujących: brak pauzy po starcie, aktualizacje tylko z opóźnieniem.

## 2. Token Chicken

| Parametr | Wartość |
|---|---|
| Nazwa / symbol | PumpChicken / **Chicken** |
| Standard | ERC-20 |
| Podaż początkowa | **1 000 000 Chicken** (przyznane twórcy przy wdrożeniu) |
| Sieć | Robinhood Chain, chainId 4663 |
| Podatek | **1 % od kupna i 1 % od sprzedaży** |
| Mint | Opcjonalny, z limitem (patrz pkt 5) |

Metadane (logo, strona, Twitter, Telegram, opis) są zapisane w kontrakcie i dostępne przez API, dzięki czemu agregatory i eksplorator mogą je odczytać po wdrożeniu. Logo może być też hostowane na IPFS.

## 3. Handel i `startTrading()`

1. **Faza przed startem.** Handel jest wstrzymany dla ogółu. Poruszać tokenami mogą tylko: właściciel, kontrakt-matka oraz portfele z listy wczesnego dostępu (portfele HD **#1–#4000** twórcy).
2. **`startTrading()`.** Jednorazowa funkcja właściciela. Otwiera handel dla wszystkich.
3. **Po starcie.** Nie ma funkcji pauzy ani ponownego blokowania. Start jest **nieodwracalny**.

Uwaga dla uczestników: przed startem portfele z listy mogą handlować, a pozostali nie. Zasada ta jest jawna i zapisana w kodzie.

## 4. DEX i podatek

- Wbudowany DEX działa według wzoru stałego iloczynu **x · y = k**.
- Cena = ETH w puli / tokeny w puli.
- Każda transakcja kupna i sprzedaży niesie **1 % podatku**.
- **Portfele HD #1–#4000 są zwolnione z podatku.** Lista jest zarządzana przez właściciela.

### Płynność i cena startowa
Twórca ustawia proporcję puli (np. 80 % tokenów i część w ETH) oraz cenę startową. Przykład: 800 000 Chicken po 0,001 ETH wymaga 800 ETH; przy 10 ETH w puli do puli trafia 10 000 Chicken, a reszta zostaje u twórcy.

## 5. Mint z limitem

- Właściciel ustawia **limit podaży** (`setMintCap`) i może dobijać podaż do tego limitu (`mint`).
- `renounceMinting` **trwale wyłącza** mintowanie i zamraża podaż.
- Mint dotyczy tylko głównego tokena Chicken.

## 6. Booster ceny

Cena na DEX zmienia się tylko przez zmianę rezerw puli. Booster to mechanizmy bota panelu:

1. **Buyback & Burn** — okresowy wykup tokenów z puli i ich spalanie (mniej tokenów w puli → wyższa cena).
2. **Jednostronne wstrzyknięcie ETH** — dodanie ETH do puli bez tokenów.

Booster można ustawić w czasie i kierunku (podnoszenie lub opuszczanie ceny). **Nie gwarantuje wzrostu ceny** — wynik zależy od rynku i wielkości rezerw.

## 7. Kontrakt-matka dla meme tokenów

Użytkownicy tworzą własne tokeny przez stronę PumpChicken. Każdy nowy token:
- powstaje z własną pulą płynności w DEX-ie matki,
- ma ten sam model podatku 1 %, z wyjątkiem portfeli zwolnionych,
- dziedziczy metadane (logo, linki, opis).

Funkcje wczesnego dostępu i mintu dotyczą wyłącznie głównego tokena Chicken.

## 8. Bezpieczeństwo i aktualizacje

- **Proxy z timelockiem:** kontrakt można aktualizować, ale każda aktualizacja wymaga zgłoszenia i **48 godzin** oczekiwania (`proposeUpgrade` → `executeUpgrade`), z możliwością anulowania. Użytkownicy mają czas na reakcję.
- **`freezeUpgrades`:** trwale wyłącza aktualizacje.
- **Brak pauzy:** po `startTrading()` nie da się zatrzymać handlu.
- **Multisig:** zalecane przeniesienie administracji proxy (`changeProxyAdmin`) i własności (`transferOwnership`) na portfel multisig.
- **Weryfikacja:** kod źródłowy jest weryfikowalny na eksploratorze Robinhood Chain.
- **Audyt:** wykonaj niezależny audyt przed mainnetem.

## 9. Panel administracyjny

Panel `/chicken` obsługuje: wdrożenie (implementacja + proxy), metadane i IPFS, płynność, booster, mint, listę zwolnionych portfeli, aktualizacje kontraktu, weryfikację, eksport dla Uniswap Assets, portfele HD #1–#4000, serie swapów (kup / sprzedaj, wg % salda lub ilości) oraz połączenie portfela przez MetaMask lub QR (WalletConnect).

## 10. Plan rozwoju

| Etap | Zakres |
|---|---|
| 1 | Wdrożenie kontraktów, weryfikacja, metadane |
| 2 | Płynność, `startTrading()`, pierwsze zasilanie puli |
| 3 | Strona użytkowników do tworzenia meme tokenów |
| 4 | Listingi i agregatory (CoinMarketCap, CoinGecko, Uniswap token list) |
| 5 | Audyt zewnętrzny, przeniesienie administracji na multisig |

## 11. Ryzyka

- Błędy w kodzie inteligentnego kontraktu, także po audycie.
- Niska płynność i duża zmienność ceny.
- Uprawnienia właściciela (lista zwolnionych, mint do limitu, aktualizacje po 48 h) wymagają zaufania do twórcy, dopóki nie zostaną ograniczone przez `renounceMinting`, `freezeUpgrades` i multisig.
- Zmiany regulacyjne.

## 12. Kontakt i linki

Uzupełnij po wdrożeniu: strona, Twitter/X, Telegram, adres launchpada, adres tokena, link do eksploratora.
