# SculptBase

分割済みの高密度パーツを「**四角面ベース + Multires + 接合部保護マスク**」に変換する Blender アドオンです。
分割後の詳細造形(sculpt フェーズ)を、軽いデータのままマルチレゾリューションで行えるようにします。

対応 Blender: 4.2 以降（拡張機能形式 / 開発・検証は Blender 5.0）。

## 背景

「形状作成 → 分割 → **リトポ → 詳細造形** → 出力」ワークフローの「リトポ→転写」を1クリック化するツールです。
詳細造形を分割後に移すことで:

- スカルプトモードの undo はストローク単位 → オブジェクトモードの全シーンコピー undo による1秒フリーズが起きない
- 重いディテールは「編集中の1パーツのマルチレゾ」に局所化される
- ダボ・分割面はマスクで機械的に保護され、勘合が崩れない

## パイプライン(パーツごと)

1. **四角リメッシュ** — ベースメッシュを生成。エンジンは自動検出で2種:
   - **QuadWild / Bi-MDF**(推奨): [QRemeshify](https://github.com/ksami/QRemeshify) 拡張がインストールされていれば利用。
     Pietroni et al. ["Reliable Feature-Line Driven Quad-Remeshing"](https://dl.acm.org/doi/10.1145/3450626.3459941) (SIGGRAPH 2021) +
     [Bi-MDF ソルバー](https://github.com/cgg-bern/quadwild-bimdf) (Heistermann et al., SIGGRAPH Asia 2023)。
     シャープ特徴に沿った高品質な四角面。密度等の詳細設定は QRemeshify パネル側に従う。
   - **QuadriFlow**(フォールバック): Blender 組み込み (Huang et al., SGP 2018)。依存なしで動作。
2. **形状転写** — ベースに Multires を付け、レベルごとに
   「Multires 細分化 → 同レベルの Catmull-Clark サブディビ + シュリンクラップ (Target Project) した参照メッシュを生成 → `multires_reshape`」
   を繰り返し、元の高密度形状を各レベルへ焼き込む。検証テストでは寸法誤差 0.1% 未満。
3. **接合部保護** — 他パーツ表面から指定距離内のベース頂点を接合部(分割面・ダボ)として BVH で検出し、
   - スカルプトマスク `= 1.0`(ブラシが動かせない = 実効の保護)
   - 頂点グループ `SB_JointGuard`(可視化・再利用)
   - フェイスセット(スカルプトモードの表示・自動マスク連携)
   の三重で保護する。
4. **ソース退避** — 元の高密度パーツは `SB_Source` コレクションに移し、**ビューレイヤーから除外**
   (依存グラフの評価対象外になるため、残しても重くならない)。結果オブジェクトが元の名前を引き継ぐ。

## 使い方

1. 分割済みパーツを**全部まとめて選択**（接合部検出はパーツ同士の近接で判定するため）。
2. サイドバー（`N`）**SculptBase** タブで設定を確認して **スカルプトベースに変換**。
3. スカルプトモードで Multires レベルを上げて詳細造形。マスク済みの接合部は彫れません。
4. 手動リトポしたパーツには **接合部マスクを再検出** だけを使うこともできます（2個以上選択）。

## 設定

- **リメッシュエンジン** — QRemeshify 検出時は QuadWild を選択可能。
- **目標四角面数**（QuadriFlow 時）— ベースの面数。レベル3なら実効密度は約64倍。
- **Multires レベル** — 焼き込む段数（既定3）。ビューポート表示はレベル1に設定される。
- **接合部判定距離** — 他パーツ表面からこの距離以内を保護（既定 1mm 相当。シーン単位に依存）。
- **ソースを退避して残す** — 無効にすると高密度ソースを削除。

## 今後の候補

- ニューラル・クロスフィールド系の新手法（NeurCross 2024 / CrossGen, SIGGRAPH Asia 2025）のエンジン追加
- 密度マップ（EvenMesh 同様のウェイトペイント）によるベース密度の局所制御

## テスト / ビルド

```
blender -b --factory-startup --python _test/test_sculptbase.py
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

## ファイル構成

```
sculptbase/
├── __init__.py            # 拡張エントリポイント
├── blender_manifest.toml  # 拡張マニフェスト
├── core.py                # リメッシュエンジン・Multires転写・接合部検出/保護
├── operators.py           # 設定 PropertyGroup + 変換/再マスクオペレーター
├── ui.py                  # N パネル UI
└── _test/test_sculptbase.py
```
