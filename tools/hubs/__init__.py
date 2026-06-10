"""Stack-chan Grove Port A 配下の I2C MUX / hub 共通ドライバ群。

このディレクトリは SAIVerse の tool ローダー
(``tools/__init__.py:_autodiscover_tools`` の grouping subdir ロジック) で
``__init__.py`` の存在をもって「ヘルパー lib」 と判定され、 中の .py は tool
として登録されない。 ENV III 等 Unit driver から `import hubs.pahub` で参照
する。
"""
