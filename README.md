# mars-homepage

Mars 的个人主页，使用 [Astro](https://astro.build) 生成静态页面。

## 运行

```bash
npm install
npm run dev      # 本地预览
npm run build    # 生成静态页到 dist/
npm run check    # 类型检查、构建，并核对每个页面的标题与站内链接
```

## 结构

- `src/layouts/Base.astro`：页面骨架、导航与页脚（站点名称与链接暂写死在此处）。
- `src/pages/`：`index`（首页）、`about`（关于）、`articles/`（文章占位列表）。
- `public/styles/site.css`：样式。
- `scripts/check-site.mjs`：构建产物检查。

内容仍在搭建中。
