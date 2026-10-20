# 回归样本说明

真实公众号文章的原始 HTML 快照，用于「微信改版后解析是否还能跑」的离线回归测试。
**全部离线可跑，不需要联网抓取**（图片仍需联网下载）。

| 文件 | 原文 | 公众号 | 特征 |
|---|---|---|---|
| kunpeng_271bfb18.html | https://mp.weixin.qq.com/s/KD169L2GRlujDl1MTzhzgA | 海科快跑 | 6 图、21 个标题块、5 个列表、section 嵌套深 10 层 |
| edsel_7bd8ffed.html | https://mp.weixin.qq.com/s/5qg7-UxZ-_6tx-CcHwYvkw | 深度学习爱好者 | 14 图（figure+figcaption，含 1 张在 `<span><section>` 里）、彩色小标题 |
| adapt_386b4f3.html | https://mp.weixin.qq.com/s/rZnzuXr-vckvWVDnGQARlQ | 视觉语言导航 | 8 图（**2 张在 `<ol><li>` 里**）、70 个 `background-image:none` 干扰、45 个 section（深 11 层） |
| fame_b04c455e.html | https://mp.weixin.qq.com/s/IwwEDZSyxWhMTndLrAB4ZA | AIPapersDaily | 7 图（其中 1 张在 `<p>` 里）、img alt 带图注、`mp-common-profile` 公众号名片 |
| table_f7d13ad5.html | https://mp.weixin.qq.com/s/RvyL_rvx3ZxR3Y_olR8ddA | 计算机视觉研究院 | **1 个真 `<table>`（5×6，无 th / 无表头样式）**、18 图含 6 张 GIF、155 个 section（深 16 层） |

用法（离线）：

    .\.venv\Scripts\w2d.exe export --input-html samples\fame_b04c455e.html --source-url "https://mp.weixin.qq.com/s/IwwEDZSyxWhMTndLrAB4ZA" -o output --force

## 尚未覆盖的结构（拿到样本请追加）

- 背景图卡片（真带 `background-image: url(...)` 的节点）
- 视频 / 音频 / 小程序卡片
- 内联 SVG 图标
- 带 `<th>` 或 `<thead>` 的表格（目前只有「无表头样式」的表格样本，靠首行推断列头）
