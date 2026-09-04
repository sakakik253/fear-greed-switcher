# Fear & Greed 切替ダッシュボード

Crypto Fear & Greed 指数と Strategy 社（MSTR）の mNAV を使って、
BTC 現物と MSTR のどちらを持つべきかを毎朝ひと目で判断するための個人用ダッシュボード。
GitHub Actions が毎朝データを取得して GitHub Pages に配信し、判定が変わった日だけ ntfy に通知する。

- ダッシュボード: https://sakakik253.github.io/fear-greed-switcher/
- 検証レポート: [docs/backtest_2026-09-04.md](docs/backtest_2026-09-04.md)（元の方式）、[docs/backtest_mnav_2026-09-04.md](docs/backtest_mnav_2026-09-04.md)（mNAV 条件付き）、[docs/research_mstu_2026-09-04.md](docs/research_mstu_2026-09-04.md)（MSTU を使う場合）

## 判定ルール（ルール F）

| 状況 | 動作 |
|---|---|
| 指数が「極端な恐怖」（25 以下）に入った | BTC 現物を売って MSTR を買う |
| 指数が「極端な強欲」（76 以上）に入り、かつ mNAV が 1.5 以上 | MSTR を売って BTC 現物に戻す |
| 指数が「極端な強欲」でも mNAV が 1.5 未満 | 売らずに MSTR を持ち続ける |
| mNAV が 3.0 以上 | 指数に関係なく BTC 現物に戻す |
| それ以外 | 何もしない |

- mNAV は「MSTR の時価総額 ÷ 保有 BTC の価値」。公式定義（負債・優先株を含む）はこれより高く出るので、しきい値はこのページの値で見る。
- 切替は米国市場の営業日の終値で行う前提。指数は日本時間の朝 9 時に更新される。
- 上げ相場で使う前提の方式。BTC が 200 日移動平均より上かどうかをレジームとして表示するが、自動では止めない。
- 過去データでの検証結果と限界は docs のレポートを参照。投資助言ではない。

## MSTU（日次 2 倍 ETF）の目安

MSTU は横ばいでも減価する（実データでの実測はおよそ年率 36% 相当）ため、次の条件のときだけ目標比率（既定 50%）で持ち、それ以外は 0% にして MSTR に寄せる。

| 条件 | 判定 |
|---|---|
| ルール F が MSTR 側、かつ BTC が 200 日移動平均の +3% 上で強気 | MSTU を目標比率で持つ |
| ルール F が BTC 側（極端な強欲かつ mNAV 1.5 以上、または mNAV 3.0 以上） | MSTU 0%、MSTR 100% |
| BTC が 200 日移動平均の -3% 下で弱気 | MSTU 0%、MSTR 100%（設定で現金にもできる） |

帯の中では直前の判定を維持する。MSTR の 100 日移動平均は参考表示。目標比率と弱気時の行き先は設定で変えられる。

## サイクル位置（4 年周期）

半減期からの経過日数で局面を分け、過去サイクルの日数と比べて現在位置を表示する。

| 局面 | 範囲（既定） | 弱気トレンドで MSTU を売った分の行き先 |
|---|---|---|
| 上昇局面 | 半減期から 550 日まで | MSTR |
| 下落局面 | 半減期 +550〜950 日 | 現金 |
| 底打ち・回復局面 | 半減期 +950 日以降、次の半減期まで | MSTR |

過去 3 回は半減期から 525〜546 日で天井、天井から 364〜410 日で底、底から 1,050〜1,068 日で次の天井だった（2012 年サイクルは価格データがないため公開情報の固定値）。
画面では、直近の天井と底からの経過日数、過去の日数を当てはめた底や次の天井の目安、過去サイクルを半減期起点で重ねたチャートを表示する。局面の日数は設定で変えられる。

## 画面の見方

| 区画 | 内容 |
|---|---|
| 今日のアクション | 自分の保有状態とルールの判定を比べて「ホールド」「MSTR へ切替」「BTC へ切替」を表示 |
| Fear & Greed 指数 | ゲージ、前日、同じ分類の連続日数 |
| MSTR の mNAV | 割安 1.0 / 出口 1.5 / 強制退出 3.0 の目盛り付きメーター、1 年中央値、公式定義の値 |
| トレンド判定 | BTC と 200 日移動平均 ±3% の帯、MSTR と 100 日移動平均 |
| MSTU の目安 | 推奨する MSTU / MSTR / 現金の比率と、変更の条件 |
| サイクル位置 | 半減期からの日数と局面、天井・底からの経過、過去サイクルとの比較チャート |
| チャート | 指数と切替点、mNAV、BTC と MSTR の相対推移、4 戦略の資産曲線。90 日 / 1 年 / 全期間 |
| 切替履歴と成績 | ルールの切替日と、2020-08-11 からの倍率・最大下落 |
| 自分の保有状態 | BTC / MSTR / MSTU と MSTR の 3 モード。MSTU モードでは現在の比率を入れると増減の指示が出る。端末内に保存し、JSON で書き出し・読み込みできる |
| 設定 | しきい値の変更（この端末だけに反映）、テーマ |

「ライブ更新」で指数と BTC 価格をブラウザから直接取得して上書きする。スマホでは「ホーム画面に追加」するとアプリのように開ける。

## セットアップ

1. **GitHub Pages を有効にする**: リポジトリの Settings → Pages → Build and deployment の Source を「GitHub Actions」にする（初回のワークフロー実行で自動設定されることもある）。
2. **ワークフローを実行する**: Actions → 「日次更新とデプロイ」→ Run workflow。成功すると上記 URL で公開される。以後は毎朝 9 時 15 分（日本時間）に自動更新。
3. **通知（任意）**: スマホに ntfy アプリを入れ、任意のトピック名を購読する。リポジトリの Settings → Secrets and variables → Actions に `NTFY_TOPIC` という名前でそのトピック名を登録する。Run workflow で「ntfy にテスト通知を送る」にチェックを入れて実行すると届くか確認できる。

通知が届くのは、ルールの望ましいポジションが変わった日と MSTU 比率の目安が変わった日（優先度 高）、指数が極端ゾーンに出入りした日、mNAV がしきい値をまたいだ日、BTC のトレンド判定が転換した日、MSTR が 100 日移動平均をまたいだ日、4 年周期の局面が変わった日だけ。

## 構成

```text
index.html                     画面（データはビルド時に埋め込む）
scripts/fetch_data.py          指数（alternative.me）、BTC / MSTR / USDJPY 日足（Yahoo Finance、フォールバックあり）
scripts/fetch_mstr_fundamentals.py  mNAV 日次データ（mnav.com）
scripts/build_site.py          判定・資産曲線を計算して dist/ を生成
scripts/notify.py              ntfy 通知
scripts/fetch_extra.py         研究用の追加銘柄取得（MSTU など）
scripts/backtest.py            元の方式のバックテスト（pandas / matplotlib が必要）
scripts/backtest_mnav.py       mNAV 条件付きのバックテスト
data/                          データのスナップショット（日次ジョブはここを更新せず、生成物だけを配信する）
docs/                          検証レポートと図
.github/workflows/deploy.yml   毎朝の取得・生成・通知・デプロイ
```

## ローカルで動かす

```bash
python3 scripts/fetch_data.py                 # データ更新（省略すると data/ のスナップショットを使う）
python3 scripts/fetch_mstr_fundamentals.py
python3 scripts/build_site.py                 # dist/ を生成
python3 -m http.server -d dist 8000           # http://localhost:8000/ で確認
```

取得・生成・通知のスクリプトは標準ライブラリだけで動く。バックテストは `pip install pandas matplotlib` が必要。

## データソース

| データ | 出典 | 更新 |
|---|---|---|
| Fear & Greed 指数 | alternative.me | 毎日 00:00 UTC |
| BTC-USD、MSTR、USDJPY | Yahoo Finance（フォールバック: CryptoCompare、Stooq）。BTC は 2014 年から | 日足 |
| mNAV、保有 BTC、発行株数、購入台帳 | mnav.com | 日次 |
