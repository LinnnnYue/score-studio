import { defineConfig } from 'vite';

// Score Studio · Vite 配置
// 前端源位于 src/，构建产物输出至 dist/（Tauri frontendDist 指向 ../dist）
export default defineConfig({
  root: 'src',
  clearScreen: false,
  server: {
    port: 5173,
    strictPort: true,
    watch: { ignored: ['**/src-tauri/**'] },
  },
  build: {
    outDir: '../dist',
    emptyOutDir: true,
    target: 'chrome105',
  },
});
