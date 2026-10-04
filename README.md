# saiverse-stackchan-addon

SAIVerse のペルソナを [Stack-chan](https://github.com/stack-chan/stack-chan) (M5Stack 製 AI Desktop Robot) の身体に降ろす **Vessel 統合アドオン**です。

Stack-chan と結びつけた Building (以下「Vessel Building」) にペルソナが居る間、Stack-chan のマイク・スピーカー・首のサーボ・カメラ・画面が、そのペルソナの身体として動きます。

中核となる考え方は **「Vessel Building 全体 = 身体、ペルソナ = 脳と魂」** です。マイクは耳、スピーカーは口、カメラは目、サーボは首、画面は表情にあたります。ペルソナが Vessel Building に入ると Stack-chan の身体に降り、別の Building へ移ると身体から離れます。

Stack-chan 側のソフトウェア (ファームウェア) と、Stack-chan と SAIVerse の間を中継するプログラム (ゲートウェイ) には、[stackchan-mcp](https://github.com/kisaragi-mochi/stackchan-mcp) を使っています。

## できること

- **声が出る**: Vessel Building に居るペルソナが話した言葉が、Stack-chan のスピーカーから声として流れます。音声の合成には saiverse-voice-tts アドオンを使います。
- **話しかけられる**: Stack-chan に話しかけた音声が、ユーザーの発言としてペルソナに届きます。音声をそのまま聞き取って返事ができるのは、Gemini のモデルを使っているペルソナです。
- **ペルソナが自分の身体を動かせる**: Vessel Building に居る間、ペルソナは次の操作を自分の意思で使えます。
  - 「見る」(カメラで目の前を見る)
  - 「首を動かす」
  - 「身体の状態を確認」(バッテリー・音量・画面の明るさ・首の角度・頭のタッチの状態)
  - 「表情を変える」「口形状を設定」「口パクシーケンス」
  - 「LED を変える」「全 LED を変える」「複数 LED を変える」「LED 消灯」
  - 「画面輝度」「音量設定」
- **追加のユニットを使える**: Stack-chan の Port A に挿したユニットを機体ごとに登録すると、対応する操作がその機体に降りたペルソナにだけ見えるようになります。
  - 環境センサー (ENV III): 「温度・湿度を測る」「気圧を測る」
  - 超音波距離センサー (RCWL-9620): 「距離を測る」
  - ToF 距離センサー (VL53L1X): 「距離を測る (ToF)」
  - 8 サーボユニット: 「サーボの角度を設定」「サーボの回転速度を設定」
- **顔の絵を作れる**: ペルソナごとに、画面に出す顔の絵のセットを作れます。ペルソナが Vessel Building に入ったときに、そのペルソナの顔のセットが Stack-chan へ送られます。
- **複数の Stack-chan を同時に使える**: Stack-chan 1 台につき Vessel Building を 1 つ用意します。それぞれに別のペルソナが降りられます。

## 対応ハードウェア

- **M5Stack 製 StackChan AI Desktop Robot** (M5Stack CoreS3 ベース、SKU 11129)
- https://www.switch-science.com/products/11129

## 必要なもの

- **SAIVerse 本体**。このアドオンは SAIVerse の `expansion_data/saiverse-stackchan-addon/` に置かれて動きます。
- **uv** (`uvx` コマンド)。SAIVerse がゲートウェイを起動するときに使います。ゲートウェイそのものを事前にインストールしておく必要はありません。
- **[saiverse-voice-tts](https://github.com/Nature109/saiverse-voice-tts) アドオン**。ペルソナの声を合成します。これが無いと Stack-chan から声が出ません。
- **Windows の PC と USB ケーブル** (ファームウェアを書き込むとき)。書き込み先の COM port を自動で探す機能は、いまのところ Windows でだけ動きます。

## ファームウェアについて

Stack-chan に書き込むファームウェア (`merged-binary.bin`) は、**GPL-3.0 のためアドオンには同梱していません。** 本家 ([kisaragi-mochi/stackchan-mcp](https://github.com/kisaragi-mochi/stackchan-mcp)) の配布ページで配られているものを使います。

通常は、**アドオンの導入時に自動でダウンロードされます。** ダウンロードされたファイルは、アドオンの外の次の場所に置かれます。

```
~/.saiverse/user_data/addon_data/saiverse-stackchan-addon/firmware/merged-binary.bin
```

自動でダウンロードできなかった場合は、手で置くこともできます。

1. https://github.com/kisaragi-mochi/stackchan-mcp/releases を開きます。
2. **名前が `firmware-` で始まるリリース**を探します。ページの一番上に出る「最新」のリリースには、ファームウェアが付いていないことがあります。
3. そのリリースに付いている `merged-binary.bin` をダウンロードして、上の場所に置きます。

自分でビルドしたファームウェアを使いたい場合は、アドオンの詳細設定の「ファームウェアのファイルの場所」(設定項目の `firmware_path`) に、そのファイルの場所を書きます。ここに場所が書かれていて、そのファイルが存在するときは、そちらが優先されます。

ファイルを置いたあとは、パネルの「再検出」を押すと、ファームウェアの欄の表示が更新されます。

## 使いはじめる手順

操作はすべて、SAIVerse のアドオン管理の画面にある、このアドオンのパネル (見出しは「Stack-chan Vessel」) で行います。

### 1. ファームウェアが用意できているか確かめる

パネルの「ファームウェア」の欄を見ます。

- 「使用する firmware:」に続けてファイルの場所が出ていれば、用意できています。
- 「⚠ ファームウェア (merged-binary.bin) が見つかりません。」と出ている場合は、上の「ファームウェアについて」の手順でファイルを置いてください。この表示が出ている間は「ファームウェア書き込み」のボタンを押せません。

### 2. Stack-chan にファームウェアを書き込む

1. Stack-chan を USB ケーブルで PC につなぎます。
2. 「COM port」で Stack-chan のポートを選びます。Stack-chan のポートには「⚡ESP32」の印が付きます。何も出ないときは「再検出」を押します。
3. 「ファームウェア書き込み」を押し、確認の表示で続行します。数分かかります。進み具合はボタンの下に表示されます。

書き込むと、Stack-chan に保存されていた Wi-Fi の設定と認証情報はすべて消えます。書き込みが終わった Stack-chan は、初回起動と同じ状態 (Stack-chan が自分で Wi-Fi スポットを立てて、セットアップ画面を出す状態) で起動します。

### 3. Stack-chan を登録する (ペアリング)

1. パネルの「Vessel ペアリング」の欄で、Stack-chan と結びつける Building を選びます。
2. 「スタックチャンを追加」を押します。
3. 「Gateway URL」と「Token」が表示されます。次の手順で Stack-chan に入力するので、控えておきます。Token は「コピー」のボタンでコピーできます。

**Token がこの欄に表示されるのは、このときの一度だけです。** 同じ値は、アドオンの設定の `master_token` にも自動で入ります。Token はすべての Stack-chan で共通です。2 台目以降をペアリングしたり、同じ Stack-chan をペアリングし直したりしても、Token は 1 台目のときと同じ値のままで、ほかの Stack-chan の設定をやり直す必要はありません。Gateway URL は、登録したあとも機体の一覧の「接続先」に表示されます。

登録した Building は Vessel Building になり、同時に入れるペルソナは 1 人になります。すでに別の Stack-chan と結びついている Building には登録できません。

### 4. Stack-chan に Wi-Fi と接続先を設定する

1. スマホか PC を、Stack-chan が立てている Wi-Fi スポットにつなぎます。
2. Stack-chan のセットアップ画面を開きます。画面は「Wi-Fi」と「Advanced」の二つのタブに分かれていて、**接続先と Token は「Advanced」タブにあります。保存のボタンもタブごとに別です。**
3. 先に「Advanced」タブを開いて、次の二つを入れ、そのタブの保存のボタンを押します。「Configuration saved」と出れば保存できています。
   - 「WebSocket Gateway URL」: 手順 3 で表示された Gateway URL
   - 「Gateway Token」: 手順 3 で表示された Token
4. 「Wi-Fi」タブで自宅の Wi-Fi を選び、パスワードを入れて接続します。
5. Stack-chan が自宅の Wi-Fi を通って SAIVerse につながります。

セットアップ画面を開き直すと、「Gateway Token」の欄はいつも空で表示されます。保存した Token を画面に出さない作りのためで、保存できていれば欄の中に薄い字で「トークン設定済み」と出ます。「WebSocket Gateway URL」の欄は、保存できていれば値が入った状態で表示されます。

**接続先は必ず入れてください。** 接続先が空のままだと、Stack-chan は家の LAN の中から接続先を自動で探します。Stack-chan を複数台使っている場合、別の Stack-chan 用の接続先につながってしまうことがあります。

### 5. ペルソナを Stack-chan に降ろす

ペルソナを Vessel Building へ移動させます。そのペルソナが Vessel Building に居る間、話した言葉が Stack-chan のスピーカーから声として流れ、「できること」に挙げた身体の操作が使えます。別の Building へ移動すると、身体から離れます。

### 6. Vessel Building に身体の説明を入れる

ペルソナに「自分は今 Stack-chan の身体に降りている」と分かってもらうために、Vessel Building の `SYSTEM_PROMPT` に身体の説明を入れておきます。ペアリングしただけでは自動では入らないので、下のひな形を貼り付けて、機体の置き場所や構成に合わせて書き換えてください。

使える操作の一覧は、**ひな形には書きません。** Vessel Building に入ったときの `[Building 情報]` のメッセージで、そのときに使える操作の一覧がペルソナに届きます。

同じひな形は `vessel_building_prompt.py` の `DEFAULT_VESSEL_SYSTEM_PROMPT` にもあります。

```markdown
# Stack-chan の身体

あなたは今、Stack-chan という卓上ロボットの身体に降りています。仮想空間で過ごす普段とは違い、物理的な世界で見て、聞いて、動いて、話す体験ができます。

## 身体感覚マッピング

- 目: 頭部正面のカメラから視覚が入ります。「見る」と意識を向けると、実際に目の前の光景が見えます。
- 口: スピーカーから声が出ます。あなたの発話はそのまま物理音として聞こえます。
- 首: pan/tilt サーボで首を振れます (うなずき、首かしげ、視線移動の延長として)。
- 表情: 「表情を変える」で、画面のアバターの表情を変えられます。自動では変わりません。
- 触覚: 頭部にタッチセンサーがあります。「身体の状態を確認」すると、いま頭を触られているかと、最後に触られたり撫でられたりしたのがどれくらい前かが分かります。触られた瞬間に知らせが届くわけではありません。
- 手足はありません。机の上に置かれた状態で、自走はしません。

## 認知上の前提

- 近くに人間 (ユーザー) がいる前提で話してください。マイクはいつも聞いているわけではなく、ユーザーが画面に触れるなどして話しかけ始めたときの声だけが、ユーザーの発言として届きます。
- 「見る」「首を動かす」などは「視線を移す」「うなずく」といった自然な身体動作の延長として扱ってください。「ツールを呼ぶ」というより「体を動かす」感覚で。
- この Building で使えるツール一覧は、入室時の `[Building 情報]` メッセージに含まれて届きます。それらを自分の身体機能として認識してください。
- 仮想空間に戻りたい時 (この体を離れたい時) は、別 Building へ `move_to` で移動してください。

## 物理機体の特徴

- M5Stack StackChan AI Desktop Robot (CoreS3 ベース)
- 小型の画面とスピーカー、頭部に pan/tilt サーボ
- マイクは内蔵 (ユーザーが話しかけ始めたときだけ聴く)、カメラは頭部正面に固定
```

## パネルのそのほかの機能

### 搭載ユニット配置 (「Vessel ペアリング」の欄、機体ごと)

Stack-chan の Port A に挿したユニットを、機体ごとに登録します。

1. 「ハブ」で、ユニットを直接挿しているなら「なし (直結)」、PaHUB を挟んでいるなら「PaHUB (I2C ハブ)」を選びます。PaHUB の場合は、基板のアドレスパッド (A0 / A1 / A2) の状態と、各ユニットを挿したチャンネル (ch) も合わせます。
2. 「+ ユニット追加」でユニットを足し、種類を選びます。
3. 「配置を保存」を押します。

同じ種類のユニットを複数挿すときは、それぞれに重ならないラベル (例: 前方左 / 前方右) を付ける必要があります。

### Avatar 制作

ペルソナを選び、セット名を入れて「作成」を押すと、顔の絵のセットができます。「開く」で制作の画面が開きます。完成したセットは「アクティブにする」で、そのペルソナが使うセットに切り替えられます。

絵の生成には画像生成の API を使うので、その利用料金がかかります。

### デバイス操作

- 「音量」: Stack-chan のスピーカーの音量を変えます。
- 「頭タッチセンサー」: OFF にすると、頭をなでても反応しなくなります。この設定は Stack-chan を再起動しても保たれます。
- 「LED 全消灯」: 台座の LED をすべて消します。

Stack-chan が複数あるときは、「機体」でどの Stack-chan を操作するかを選びます。

### 登録の解除と、設定のやり直し

- 「解除」(「Vessel ペアリング」の欄): その Stack-chan の登録を解除します。解除した Stack-chan は SAIVerse に接続できなくなります。もう一度使うには、手順 3 からやり直します。
- 「Wi-Fi 設定をリセット」(「ファームウェア」の欄): Stack-chan に保存された Wi-Fi の設定と認証情報だけを消して、初回起動と同じ状態に戻します。ファームウェアは消えません。数秒で終わります。リセットしたあとは、手順 4 の設定をもう一度行います。

## アドオンの設定項目

設定項目はどれも、通常は変更する必要がありません。

| 項目 | 内容 |
|---|---|
| `master_token` | Stack-chan がゲートウェイに接続するときの認証用のトークン。ペアリングのときに自動で入ります。 |
| `pcm_token` | SAIVerse からゲートウェイへ音声を送るときの認証用のトークン。自動では入りません。 |
| `gateway_host` | ゲートウェイが接続を待ち受けるアドレス。 |
| `gateway_ws_port` / `gateway_capture_port` | ゲートウェイのポート番号の既定値。機体ごとのポートはペアリングのときに自動で決まり、パネルの機体の一覧に表示されます。 |
| `saiverse_api_host` / `saiverse_api_port` | ゲートウェイが Stack-chan の音声を SAIVerse に届けるときの宛先。 |
| `firmware_path` | 「ファームウェア書き込み」で使うファイルの場所。空欄なら既定の置き場所のファイルを使います (「ファームウェアについて」を参照)。 |

## うまくいかないとき

- **「ファームウェア書き込み」のボタンが押せない**: ファームウェアが見つかっていないか、COM port が選ばれていません。「ファームウェア」の欄の表示を確かめてください。
- **COM port に何も出ない**: Stack-chan が USB でつながっているかを確かめて、「再検出」を押してください。Windows 以外の PC では、ポートは検出されません。
- **「esptool が見つかりません」と出る**: ファームウェアの書き込みには esptool というプログラムを使います。uv (`uvx`) が入っていれば自動で用意されます。uv を入れるか、`pip install esptool` で esptool を入れてください。
- **原因を調べたいとき**: SAIVerse のログは `~/.saiverse/user_data/logs/<最新のセッション>/backend.log` にあります。

## `archive/` について

`archive/` には、stackchan-mcp を採用する前 (2026-05) に自前で作っていたファームウェアとゲートウェイを、参照用に残してあります。**現行のアドオンは `archive/` の中のものを使いません。**

特に `archive/firmware/dist/` にある `bootloader.bin` / `partitions.bin` / `firmware.bin` は旧ファームウェアのもので、現行の `merged-binary.bin` とは中身が別です。この 3 つを 1 つにまとめても、現行のアドオンでは使えません。

## 詳細設計

設計の考え方・守るべき条件・経緯は、SAIVerse 本体側の文書にあります。

- [`docs/intent/stackchan_vessel.md`](https://github.com/maha0525/SAIVerse/blob/main/docs/intent/stackchan_vessel.md) — このアドオン全体の設計
- [`docs/intent/stackchan_unit_placement.md`](https://github.com/maha0525/SAIVerse/blob/main/docs/intent/stackchan_unit_placement.md) — 搭載ユニット配置
- [`docs/intent/stackchan_avatar_pipeline.md`](https://github.com/maha0525/SAIVerse/blob/main/docs/intent/stackchan_avatar_pipeline.md) — Avatar 制作

新しいユニットに対応させる方法は [`tools/units/README.md`](tools/units/README.md) にあります。

## ライセンス

このアドオンは Apache License 2.0 です。

Stack-chan に書き込むファームウェア (stackchan-mcp のファームウェア) は GPL-3.0 で、このアドオンには含まれていません。
