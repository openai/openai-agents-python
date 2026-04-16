# 截图指南

## 📸 需要截图的场景

### 1. 启动画面
```
============================================================
 AML Compliance Agent - Local Gemma 2B (Offline)
============================================================

Loading Gemma 2B model...
Model loaded!
```

**截图要点**：显示模型加载成功的信息

---

### 2. 制裁筛查步骤
```
[1/4] Running sanctions screening...
      Result: Risk Level: low
             Recommended Action: clear
             Details: No sanctions matches found...
```

**截图要点**：显示步骤1完成，风险等级为low

---

### 3. 风险评估步骤
```
[2/4] Performing risk assessment...
      Result: Overall Risk: low
             Review Frequency: annual
             Justification: Standard customer profile...
```

**截图要点**：显示步骤2完成，整体风险为low

---

### 4. 交易分析步骤
```
[3/4] Analyzing transactions...
      No alerts generated
```

**截图要点**：显示步骤3完成，无预警

---

### 5. 报告生成步骤
```
[4/4] Generating compliance report...
      Report: Status: compliant
             Next Review: 2026-04-16
             Summary: Customer cleared all checks...
```

**截图要点**：显示步骤4完成，状态为compliant

---

### 6. 完成画面
```
============================================================
Compliance Check Complete (100% Offline)
============================================================

Key Features:
  ✓ 100% offline - no data leaves your machine
  ✓ Local Gemma 2B model
  ✓ Complete AML workflow
  ✓ Privacy-preserving compliance checks
```

**截图要点**：显示"100% Offline"完成信息

---

## 🎬 视频录制指南

### 录制步骤

1. **打开终端**
   ```bash
   cd projects/openai-agents
   ```

2. **开始录制** (Windows: Win + Alt + R)

3. **运行命令**
   ```bash
   python -m examples.aml_compliance_agent.main_gemma
   ```

4. **解说要点** (3-4分钟)
   - "这是一个完全离线的AML合规Agent"
   - "使用本地Gemma 2B模型，数据不出机器"
   - "演示4步合规流程：筛查→评估→监控→报告"
   - "适合银行、金融科技公司的隐私场景"

5. **结束录制** (Win + Alt + R)

### 视频保存位置
```
C:\Users\jie13\Videos\Captures\
```

---

## 📋 截图命名

| 截图 | 文件名 |
|------|--------|
| 启动 | `screenshot-01-start.png` |
| 筛查 | `screenshot-02-screening.png` |
| 评估 | `screenshot-03-risk.png` |
| 监控 | `screenshot-04-monitoring.png` |
| 报告 | `screenshot-05-report.png` |
| 完成 | `screenshot-06-complete.png` |

---

## 🎯 提交用途

- **GitHub PR**: 添加到 PR 描述中
- **README**: 嵌入到文档中
- **Devpost**: Gemma 4 Good 比赛提交

---

**提示**: 如果终端输出太快，可以在代码中添加 `time.sleep(1)` 暂停
