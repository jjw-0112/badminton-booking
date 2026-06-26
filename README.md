# 移动交通大学羽毛球场自动预订脚本

这个项目通过 MuMu 模拟器自带的 ADB 控制安卓 App，适用于“移动交通大学”App 内的体育场馆预订服务。脚本默认是 `dry-run`，只会走到可下单前；只有显式加 `--execute` 或把配置里的 `run.execute` 改成 `true`，才会点击“我要下单”。

验证码不会被自动破解。正式执行时，如果出现滑块验证码，脚本会提醒你在 MuMu 里手动完成，然后继续等待结果。

## 使用前准备

1. 打开 MuMu 模拟器。
2. 登录“移动交通大学”App。
3. 手动进入“体育场馆预订服务”的“关注场地”首页。
4. 确认配置文件 `booking_config.json` 里的场馆、日期和候选场次符合你的需求。

默认配置：

- 场馆：以 `booking_config.json` 的 `booking.venue` 为准
- 定时时间：`08:40:00`
- 日期：`date_offset_days = 1`，也就是明天；如果设置了 `target_date`，则优先使用 `target_date`
- 默认不正式下单：`run.execute = false`

## 命令

在 VSCode 里直接点击运行 `badminton_booking.py` 时，脚本会默认执行：

```powershell
python badminton_booking.py run --mode once
```

也就是 dry-run 到可下单前，不会点击“我要下单”。

检查环境和当前页面：

```powershell
python badminton_booking.py check
```

立即 dry-run：

```powershell
python badminton_booking.py run --mode once
```

立即正式执行：

```powershell
python badminton_booking.py run --mode once --execute
```

定时正式执行，默认使用配置里的 `08:40:00`：

```powershell
python badminton_booking.py run --mode schedule --execute
```

临时覆盖定时时间：

```powershell
python badminton_booking.py run --mode schedule --time 08:39:50 --execute
```

## 修改目标场次

修改场馆时，只改 `booking_config.json` 的 `booking.venue`。候选场次默认跟随这个场馆。

编辑 `booking_config.json` 的 `booking.priorities` 可以修改时间和场地。每个候选项最多两个时间段，两个时间段必须是同一场地且连续：

```json
{
  "name": "首选-场地1-09:00到11:00",
  "court": "场地1",
  "times": ["09:00-10:00", "10:00-11:00"]
}
```

脚本会按列表顺序尝试。首选不可用时，会继续尝试下一个候选项。

## 校准坐标

学校 App 的预订页是 H5/WebView，很多按钮和表格无法通过系统控件文字读取，所以脚本用截图颜色和坐标配合判断。若页面位置和默认配置不一致，运行：

```powershell
python badminton_booking.py calibrate
```

命令会保存当前截图到 `calibration_screen.png`，并提示你输入关键坐标。留空表示保持原配置。

## 注意事项

- 先用 `check` 和 dry-run 验证，不要一开始就正式执行。
- 如果页面识别为 `unknown`，脚本会按配置假定当前在关注场地首页；如果你实际不在这个页面，可能会点错位置。
- 如果场次颜色识别不准，可以先校准坐标；必要时再调整 `colors` 里的颜色阈值。
- 脚本只用于你自己账号的正常预订流程，不绕过学校规则，不破解滑块验证码。
