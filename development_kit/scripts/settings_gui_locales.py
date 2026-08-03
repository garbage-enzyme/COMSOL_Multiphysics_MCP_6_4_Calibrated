"""Generate and verify deterministic gettext catalogs for the Settings GUI."""

# ruff: noqa

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from settings_gui.i18n import MESSAGE_IDS

LOCALE_ROOT = ROOT / "settings_gui" / "locales"
DOMAIN = "settings_gui"

ZH_CN = {
    "COMSOL MCP Settings": "COMSOL MCP 设置",
    "General": "常规",
    "Profile": "工具配置",
    "Runtime": "运行路径",
    "COMSOL/Java": "COMSOL/Java",
    "Shared": "共享",
    "Evidence": "证据",
    "Docs": "文档",
    "Owner": "所有者",
    "About": "关于",
    "Changes take effect only after restarting Codex or the owning MCP client.": "改动仅在重启 Codex 或当前 MCP 客户端后生效。",
    "Save and Exit": "保存并退出",
    "Apply": "应用",
    "Cancel": "取消",
    "Browse": "浏览",
    "Clear": "清空",
    "Auto-detect": "自动检测",
    "Add folder": "添加文件夹",
    "Remove": "移除",
    "Choose folder": "选择文件夹",
    "Choose file": "选择文件",
    "GUI release": "GUI 发布版本",
    "Installed package version": "已安装软件包版本",
    "Desktop shortcut": "桌面快捷方式",
    "Create desktop shortcut": "创建桌面快捷方式",
    "Remove desktop shortcut": "移除桌面快捷方式",
    "Replace existing desktop shortcut?": "要替换现有桌面快捷方式吗？",
    "A different shortcut already uses this name. Replace it?": "已有其他快捷方式使用此名称。是否替换？",
    "Desktop shortcut ready": "桌面快捷方式已就绪",
    "The desktop shortcut now opens this exact settings file.": "此桌面快捷方式现在会打开当前这一设置文件。",
    "Desktop shortcut could not be created": "无法创建桌面快捷方式",
    "The existing Desktop item was preserved.": "现有桌面项目已保留。",
    "Desktop shortcut removed": "桌面快捷方式已移除",
    "The owned desktop shortcut was removed.": "本应用创建的桌面快捷方式已移除。",
    "No owned desktop shortcut was found.": "未找到本应用创建的桌面快捷方式。",
    "Desktop shortcut not removed": "未移除桌面快捷方式",
    "The Desktop item is not owned by this application and was preserved.": "该桌面项目不是由本应用创建的，已予保留。",
    "Repositories and acknowledgements": "仓库与致谢",
    "This repository": "当前仓库",
    "Thanks: upstream project": "感谢上游项目",
    "Thanks: Ching-Chiang project": "感谢 Ching-Chiang 项目",
    "MIT License": "MIT 许可证",
    "English": "English",
    "简体中文": "简体中文",
    "繁體中文": "繁體中文",
    "Internal settings format name. You do not need to change it.": "设置文件内部使用的格式名称，不需要修改。",
    "Settings format version used when this file is saved.": "保存文件时使用的设置格式版本。",
    "Language used by this Settings window.": "此设置窗口使用的语言。",
    "Size of text and controls. Following Windows is recommended. Other choices are previewed immediately and saved for the next opening.": "文字和控件的大小。建议跟随 Windows；其他选项会立即预览，并在下次打开时继续使用。",
    "Follow Windows display settings": "跟随 Windows 显示设置",
    "Safety-first default for new users. It offers fewer operations, which lowers the risk of an unintended change. Choose it while learning or when you only need to open and inspect models, manage jobs, run careful single-point checks, or search manuals.": "面向新手的安全默认选项。它提供的操作较少，可降低误操作风险。学习阶段，或只需要打开和查看模型、管理任务、进行谨慎的单点检查或搜索手册时，请选择此项。",
    "Recommended for most users. It covers ordinary FEM model building and result export, and includes tools for making a Windows standalone package. Choose it for general simulation work that does not need a specialist profile.": "推荐大多数用户选择。它覆盖常规 FEM 建模和结果导出，也包含制作 Windows standalone 包的工具。一般仿真不需要专用 profile 时，请选择此项。",
    "For optical and metasurface work. Adds materials, field review, Wave Optics checks, point audits, and staged parameter workflows to Core.": "适合光学和超表面工作。在 Core 基础上增加材料、场结果查看、Wave Optics 检查、单点审计和分阶段参数流程。",
    "For searching prepared local COMSOL manuals with text and meaning-based search. Choose it only after the optional manual indexes and search model have been prepared.": "适合搜索已准备好的本地 COMSOL 手册，支持文字和语义搜索。只有在可选手册索引和搜索模型准备完成后才选择此项。",
    "For users who want to watch the same model in COMSOL Desktop while MCP takes a turn through a local Server. Choose it only after Desktop and Server are connected and shared mode is enabled.": "适合希望在 COMSOL Desktop 中观看同一模型，并让 MCP 通过本机 Server 轮流操作的用户。只有在 Desktop 与 Server 已连接且共享模式已开启后才选择此项。",
    "For testing extra helpers that are broader or less mature. Use it only when a required tool is missing from the safer profiles, and check every output carefully.": "适合测试范围更广或尚未成熟的额外工具。只有在较安全的 profile 缺少所需工具时才使用，并仔细检查每项输出。",
    "For old workflows that need nearly every tool. It keeps older broad path behavior and has weaker file containment. New users should not choose it.": "适合需要几乎全部工具的旧流程。它保留较宽松的旧路径行为，文件范围保护较弱；新用户不应选择。",
    "Folder for working files and locks. Use an ASCII-only path. \nExample: %PROGRAMDATA%\\comsol_mcp\\runtime": "存放运行文件和锁的文件夹。路径只能含 ASCII 字符。\n示例：%PROGRAMDATA%\\comsol_mcp\\runtime",
    "Optional separate folder for resumable jobs. Leave it empty to use the runtime folder. \nExample: %PROGRAMDATA%\\comsol_mcp\\runtime\\jobs": "可选的可恢复任务文件夹。留空时使用运行目录。\n示例：%PROGRAMDATA%\\comsol_mcp\\runtime\\jobs",
    "Folders where COMSOL MCP may read your .mph files. Chinese paths are supported. \nExample: %LOCALAPPDATA%\\comsol_mcp\\models": "COMSOL MCP 可以读取 .mph 文件的文件夹。支持中文路径。\n示例：%LOCALAPPDATA%\\comsol_mcp\\models",
    "Folder where COMSOL MCP writes results and evidence. Use an ASCII-only path. \nExample: %PROGRAMDATA%\\comsol_mcp\\artifacts": "COMSOL MCP 写入结果和证据的文件夹。路径只能含 ASCII 字符。\n示例：%PROGRAMDATA%\\comsol_mcp\\artifacts",
    "Folder where COMSOL Multiphysics 6.4 is installed. \nExample: C:\\COMSOL64\\Multiphysics": "COMSOL Multiphysics 6.4 的安装文件夹。\n示例：C:\\COMSOL64\\Multiphysics",
    "Java folder used by COMSOL. Auto-detect can fill this value. \nExample: C:\\COMSOL64\\Multiphysics\\java\\win64\\jre": "COMSOL 使用的 Java 文件夹，可由自动检测填写。\n示例：C:\\COMSOL64\\Multiphysics\\java\\win64\\jre",
    "JDK folder used by COMSOL. Auto-detect can fill this value. \nExample: C:\\COMSOL64\\Multiphysics\\java\\win64\\jre": "COMSOL 使用的 JDK 文件夹，可由自动检测填写。\n示例：C:\\COMSOL64\\Multiphysics\\java\\win64\\jre",
    "Allow MCP to work with a COMSOL Desktop connected to a local Server.": "允许 MCP 与连接到本机 Server 的 COMSOL Desktop 协作。",
    "Check that execution results and scientific conclusions are reported separately.": "检查运行结果和科学结论是否分开说明。",
    "Check saved result files and their hashes.": "检查保存的结果文件及其哈希。",
    "Check that summary statements match the saved result values.": "检查摘要说明是否与保存的结果数值一致。",
    "Check that a resumed job uses the same producer and driver.": "检查恢复任务是否使用相同的生成器和驱动。",
    "Optional folder containing prepared semantic-search files. This is not COMSOL's built-in manual folder. \nExample: %LOCALAPPDATA%\\comsol_mcp\\semantic\\manuals": "存放已准备语义搜索文件的可选文件夹。它不是 COMSOL 自带的 manual 文件夹。\n示例：%LOCALAPPDATA%\\comsol_mcp\\semantic\\manuals",
    "Optional SQLite file used for manual text search. \nExample: %LOCALAPPDATA%\\comsol_mcp\\semantic\\manuals.sqlite3": "用于手册文字搜索的可选 SQLite 文件。\n示例：%LOCALAPPDATA%\\comsol_mcp\\semantic\\manuals.sqlite3",
    "Optional folder containing the local semantic-search model. \nExample: %LOCALAPPDATA%\\comsol_mcp\\semantic\\models": "存放本地语义搜索模型的可选文件夹。\n示例：%LOCALAPPDATA%\\comsol_mcp\\semantic\\models",
    "Optional name that identifies who owns the COMSOL session.": "用于说明谁在使用 COMSOL 会话的可选名称。",
    "Enter a valid absolute path or clear this setting.": "请输入有效的绝对路径，或清空此项。",
    "Enter an ASCII-only full path, or leave this setting empty.": "请输入仅含 ASCII 字符的完整路径，或将此项留空。",
    "Enter a valid absolute path.": "请输入有效的绝对路径。",
    "Enter a valid value for this setting.": "请输入此设置允许的有效值。",
    "Required setting is missing.": "缺少必需设置。",
    "Unknown setting is not supported.": "不支持未知设置。",
    "Disable evidence check?": "要禁用证据检查吗？",
    "Disabling an evidence check makes future results not fully verified. Continue?": "禁用证据检查后，后续结果将不再是完整核验状态。是否继续？",
    "Restart required": "需要重启",
    "Invalid settings": "设置无效",
    "Correct every highlighted field before saving.": "请先修正所有标红字段，再保存。",
    "Save failed": "保存失败",
    "Settings were not saved. Close conflicting editors and try again.": "设置未保存。请关闭冲突的编辑器后重试。",
    "Discard changes?": "要放弃改动吗？",
    "Close without saving the current edits?": "是否放弃当前编辑并关闭窗口？",
    "The configured COMSOL root is not a supported 6.4 installation.": "当前 COMSOL 根目录不是受支持的 6.4 安装。",
    "Multiple COMSOL 6.4 installations were found. Choose one manually.": "检测到多个 COMSOL 6.4 安装，请手动选择。",
    "No supported COMSOL 6.4 installation was found.": "未找到受支持的 COMSOL 6.4 安装。",
    "Replace existing values?": "要替换现有值吗？",
    "Auto-detect would replace these settings: {keys}": "自动检测将替换这些设置：{keys}",
    "Settings conflict": "设置冲突",
    "The settings file changed outside this editor. Close this window and reopen settings.": "设置文件已被其他程序修改。请关闭此窗口并重新打开设置。",
    "The selected language catalog is unavailable; English is active.": "所选语言资源不可用，当前已切换为英文。",
    "No writable settings file exists. Rebuild canonical settings now?\n\nChoose No to exit without writing.": "尚无可写的设置文件。现在创建标准设置吗？\n\n选择“否”将直接退出且不写入文件。",
    "Another settings editor is active. Close it and reopen settings.": "另一个设置编辑器正在运行。请先关闭它，再重新打开设置。",
    "Settings could not be prepared safely.": "无法安全地准备设置文件。",
    "Settings are missing or damaged. Preserve them and rebuild defaults?": "设置文件缺失或已损坏。是否保留原文件并重建默认设置？",
}

ZH_TW = {
    "COMSOL MCP Settings": "COMSOL MCP 設定",
    "General": "一般",
    "Profile": "工具設定檔",
    "Runtime": "執行路徑",
    "COMSOL/Java": "COMSOL/Java",
    "Shared": "共用",
    "Evidence": "證據",
    "Docs": "文件",
    "Owner": "擁有者",
    "About": "關於",
    "Changes take effect only after restarting Codex or the owning MCP client.": "變更僅在重新啟動 Codex 或目前的 MCP 用戶端後生效。",
    "Save and Exit": "儲存並結束",
    "Apply": "套用",
    "Cancel": "取消",
    "Browse": "瀏覽",
    "Clear": "清除",
    "Auto-detect": "自動偵測",
    "Add folder": "新增資料夾",
    "Remove": "移除",
    "Choose folder": "選擇資料夾",
    "Choose file": "選擇檔案",
    "GUI release": "GUI 發行版本",
    "Installed package version": "已安裝套件版本",
    "Desktop shortcut": "桌面捷徑",
    "Create desktop shortcut": "建立桌面捷徑",
    "Remove desktop shortcut": "移除桌面捷徑",
    "Replace existing desktop shortcut?": "要取代現有桌面捷徑嗎？",
    "A different shortcut already uses this name. Replace it?": "已有其他捷徑使用此名稱。是否取代？",
    "Desktop shortcut ready": "桌面捷徑已就緒",
    "The desktop shortcut now opens this exact settings file.": "此桌面捷徑現在會開啟目前這個設定檔。",
    "Desktop shortcut could not be created": "無法建立桌面捷徑",
    "The existing Desktop item was preserved.": "現有桌面項目已保留。",
    "Desktop shortcut removed": "桌面捷徑已移除",
    "The owned desktop shortcut was removed.": "本應用程式建立的桌面捷徑已移除。",
    "No owned desktop shortcut was found.": "找不到本應用程式建立的桌面捷徑。",
    "Desktop shortcut not removed": "未移除桌面捷徑",
    "The Desktop item is not owned by this application and was preserved.": "該桌面項目不是由本應用程式建立的，已予保留。",
    "Repositories and acknowledgements": "程式庫與致謝",
    "This repository": "目前程式庫",
    "Thanks: upstream project": "感謝上游專案",
    "Thanks: Ching-Chiang project": "感謝 Ching-Chiang 專案",
    "MIT License": "MIT 授權條款",
    "English": "English",
    "简体中文": "简体中文",
    "繁體中文": "繁體中文",
    "Internal settings format name. You do not need to change it.": "設定檔內部使用的格式名稱，不需要修改。",
    "Settings format version used when this file is saved.": "儲存檔案時使用的設定格式版本。",
    "Language used by this Settings window.": "此設定視窗使用的語言。",
    "Size of text and controls. Following Windows is recommended. Other choices are previewed immediately and saved for the next opening.": "文字和控制項的大小。建議跟隨 Windows；其他選項會立即預覽，並在下次開啟時繼續使用。",
    "Follow Windows display settings": "跟隨 Windows 顯示設定",
    "Safety-first default for new users. It offers fewer operations, which lowers the risk of an unintended change. Choose it while learning or when you only need to open and inspect models, manage jobs, run careful single-point checks, or search manuals.": "面向新手的安全預設選項。它提供的操作較少，可降低誤操作風險。學習階段，或只需要開啟和查看模型、管理工作、進行謹慎的單點檢查或搜尋手冊時，請選擇此項。",
    "Recommended for most users. It covers ordinary FEM model building and result export, and includes tools for making a Windows standalone package. Choose it for general simulation work that does not need a specialist profile.": "建議大多數使用者選擇。它涵蓋一般 FEM 建模和結果匯出，也包含製作 Windows standalone 套件的工具。一般模擬不需要專用 profile 時，請選擇此項。",
    "For optical and metasurface work. Adds materials, field review, Wave Optics checks, point audits, and staged parameter workflows to Core.": "適合光學和超表面工作。在 Core 基礎上增加材料、場結果查看、Wave Optics 檢查、單點稽核和分階段參數流程。",
    "For searching prepared local COMSOL manuals with text and meaning-based search. Choose it only after the optional manual indexes and search model have been prepared.": "適合搜尋已準備好的本機 COMSOL 手冊，支援文字和語意搜尋。只有在可選手冊索引和搜尋模型準備完成後才選擇此項。",
    "For users who want to watch the same model in COMSOL Desktop while MCP takes a turn through a local Server. Choose it only after Desktop and Server are connected and shared mode is enabled.": "適合希望在 COMSOL Desktop 中觀看同一模型，並讓 MCP 透過本機 Server 輪流操作的使用者。只有在 Desktop 與 Server 已連線且共用模式已啟用後才選擇此項。",
    "For testing extra helpers that are broader or less mature. Use it only when a required tool is missing from the safer profiles, and check every output carefully.": "適合測試範圍更廣或尚未成熟的額外工具。只有在較安全的 profile 缺少所需工具時才使用，並仔細檢查每項輸出。",
    "For old workflows that need nearly every tool. It keeps older broad path behavior and has weaker file containment. New users should not choose it.": "適合需要幾乎全部工具的舊流程。它保留較寬鬆的舊路徑行為，檔案範圍保護較弱；新使用者不應選擇。",
    "Folder for working files and locks. Use an ASCII-only path. \nExample: %PROGRAMDATA%\\comsol_mcp\\runtime": "存放執行檔案和鎖定資料的資料夾。路徑只能含 ASCII 字元。\n範例：%PROGRAMDATA%\\comsol_mcp\\runtime",
    "Optional separate folder for resumable jobs. Leave it empty to use the runtime folder. \nExample: %PROGRAMDATA%\\comsol_mcp\\runtime\\jobs": "可選的可恢復工作資料夾。留空時使用執行目錄。\n範例：%PROGRAMDATA%\\comsol_mcp\\runtime\\jobs",
    "Folders where COMSOL MCP may read your .mph files. Chinese paths are supported. \nExample: %LOCALAPPDATA%\\comsol_mcp\\models": "COMSOL MCP 可以讀取 .mph 檔案的資料夾。支援中文路徑。\n範例：%LOCALAPPDATA%\\comsol_mcp\\models",
    "Folder where COMSOL MCP writes results and evidence. Use an ASCII-only path. \nExample: %PROGRAMDATA%\\comsol_mcp\\artifacts": "COMSOL MCP 寫入結果和證據的資料夾。路徑只能含 ASCII 字元。\n範例：%PROGRAMDATA%\\comsol_mcp\\artifacts",
    "Folder where COMSOL Multiphysics 6.4 is installed. \nExample: C:\\COMSOL64\\Multiphysics": "COMSOL Multiphysics 6.4 的安裝資料夾。\n範例：C:\\COMSOL64\\Multiphysics",
    "Java folder used by COMSOL. Auto-detect can fill this value. \nExample: C:\\COMSOL64\\Multiphysics\\java\\win64\\jre": "COMSOL 使用的 Java 資料夾，可由自動偵測填入。\n範例：C:\\COMSOL64\\Multiphysics\\java\\win64\\jre",
    "JDK folder used by COMSOL. Auto-detect can fill this value. \nExample: C:\\COMSOL64\\Multiphysics\\java\\win64\\jre": "COMSOL 使用的 JDK 資料夾，可由自動偵測填入。\n範例：C:\\COMSOL64\\Multiphysics\\java\\win64\\jre",
    "Allow MCP to work with a COMSOL Desktop connected to a local Server.": "允許 MCP 與連線到本機 Server 的 COMSOL Desktop 協作。",
    "Check that execution results and scientific conclusions are reported separately.": "檢查執行結果和科學結論是否分開說明。",
    "Check saved result files and their hashes.": "檢查儲存的結果檔案及其雜湊。",
    "Check that summary statements match the saved result values.": "檢查摘要說明是否與儲存的結果數值一致。",
    "Check that a resumed job uses the same producer and driver.": "檢查恢復工作是否使用相同的產生器和驅動程式。",
    "Optional folder containing prepared semantic-search files. This is not COMSOL's built-in manual folder. \nExample: %LOCALAPPDATA%\\comsol_mcp\\semantic\\manuals": "存放已準備語意搜尋檔案的可選資料夾。它不是 COMSOL 內建的 manual 資料夾。\n範例：%LOCALAPPDATA%\\comsol_mcp\\semantic\\manuals",
    "Optional SQLite file used for manual text search. \nExample: %LOCALAPPDATA%\\comsol_mcp\\semantic\\manuals.sqlite3": "用於手冊文字搜尋的可選 SQLite 檔案。\n範例：%LOCALAPPDATA%\\comsol_mcp\\semantic\\manuals.sqlite3",
    "Optional folder containing the local semantic-search model. \nExample: %LOCALAPPDATA%\\comsol_mcp\\semantic\\models": "存放本機語意搜尋模型的可選資料夾。\n範例：%LOCALAPPDATA%\\comsol_mcp\\semantic\\models",
    "Optional name that identifies who owns the COMSOL session.": "用於說明誰在使用 COMSOL 工作階段的可選名稱。",
    "Enter a valid absolute path or clear this setting.": "請輸入有效的絕對路徑，或清除此項。",
    "Enter an ASCII-only full path, or leave this setting empty.": "請輸入僅含 ASCII 字元的完整路徑，或將此項留空。",
    "Enter a valid absolute path.": "請輸入有效的絕對路徑。",
    "Enter a valid value for this setting.": "請輸入此設定允許的有效值。",
    "Required setting is missing.": "缺少必要設定。",
    "Unknown setting is not supported.": "不支援未知設定。",
    "Disable evidence check?": "要停用證據檢查嗎？",
    "Disabling an evidence check makes future results not fully verified. Continue?": "停用證據檢查後，後續結果將不再是完整驗證狀態。是否繼續？",
    "Restart required": "需要重新啟動",
    "Invalid settings": "設定無效",
    "Correct every highlighted field before saving.": "請先修正所有標紅欄位，再儲存。",
    "Save failed": "儲存失敗",
    "Settings were not saved. Close conflicting editors and try again.": "設定未儲存。請關閉衝突的編輯器後再試一次。",
    "Discard changes?": "要捨棄變更嗎？",
    "Close without saving the current edits?": "是否捨棄目前的編輯並關閉視窗？",
    "The configured COMSOL root is not a supported 6.4 installation.": "目前的 COMSOL 根目錄不是受支援的 6.4 安裝。",
    "Multiple COMSOL 6.4 installations were found. Choose one manually.": "偵測到多個 COMSOL 6.4 安裝，請手動選擇。",
    "No supported COMSOL 6.4 installation was found.": "找不到受支援的 COMSOL 6.4 安裝。",
    "Replace existing values?": "要取代現有值嗎？",
    "Auto-detect would replace these settings: {keys}": "自動偵測將取代這些設定：{keys}",
    "Settings conflict": "設定衝突",
    "The settings file changed outside this editor. Close this window and reopen settings.": "設定檔已被其他程式修改。請關閉此視窗並重新開啟設定。",
    "The selected language catalog is unavailable; English is active.": "所選語言資源無法使用，目前已切換為英文。",
    "No writable settings file exists. Rebuild canonical settings now?\n\nChoose No to exit without writing.": "目前沒有可寫入的設定檔。現在建立標準設定嗎？\n\n選擇「否」將直接結束且不寫入檔案。",
    "Another settings editor is active. Close it and reopen settings.": "另一個設定編輯器正在執行。請先關閉它，再重新開啟設定。",
    "Settings could not be prepared safely.": "無法安全地準備設定檔。",
    "Settings are missing or damaged. Preserve them and rebuild defaults?": "設定檔遺失或已損壞。是否保留原檔並重建預設設定？",
}


def _header(language: str) -> str:
    plural = (
        "nplurals=1; plural=0;" if language.startswith("zh_") else "nplurals=2; plural=(n != 1);"
    )
    return (
        "Project-Id-Version: comsol-mcp alpha6.1\n"
        "MIME-Version: 1.0\n"
        "Content-Type: text/plain; charset=UTF-8\n"
        "Content-Transfer-Encoding: 8bit\n"
        f"Language: {language}\n"
        f"Plural-Forms: {plural}\n"
    )


def _quoted(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return f'"{escaped}"'


def _render_po(language: str, translations: dict[str, str]) -> bytes:
    lines = [
        "# Generated deterministically; edit the generator, not this file.",
        'msgid ""',
        f"msgstr {_quoted(_header(language))}",
        "",
    ]
    for message in MESSAGE_IDS:
        lines.extend((f"msgid {_quoted(message)}", f"msgstr {_quoted(translations[message])}", ""))
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _render_pot() -> bytes:
    lines = [
        "# Generated deterministic gettext template for COMSOL MCP Settings.",
        'msgid ""',
        f"msgstr {_quoted(_header(''))}",
        "",
    ]
    for message in MESSAGE_IDS:
        lines.extend((f"msgid {_quoted(message)}", 'msgstr ""', ""))
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _compile_mo(language: str, translations: dict[str, str]) -> bytes:
    catalog = {"": _header(language), **translations}
    keys = sorted(catalog)
    encoded_ids = [key.encode("utf-8") for key in keys]
    encoded_values = [catalog[key].encode("utf-8") for key in keys]
    count = len(keys)
    original_table = 28
    translated_table = original_table + count * 8
    original_strings = translated_table + count * 8
    translated_strings = original_strings + sum(len(item) + 1 for item in encoded_ids)

    chunks = [struct.pack("<7I", 0x950412DE, 0, count, original_table, translated_table, 0, 0)]
    offset = original_strings
    for item in encoded_ids:
        chunks.append(struct.pack("<2I", len(item), offset))
        offset += len(item) + 1
    offset = translated_strings
    for item in encoded_values:
        chunks.append(struct.pack("<2I", len(item), offset))
        offset += len(item) + 1
    chunks.append(b"\0".join(encoded_ids) + b"\0")
    chunks.append(b"\0".join(encoded_values) + b"\0")
    return b"".join(chunks)


def expected_files() -> dict[Path, bytes]:
    inventories = {
        "en": {message: message for message in MESSAGE_IDS},
        "zh_CN": ZH_CN,
        "zh_TW": ZH_TW,
    }
    for language, translations in inventories.items():
        if set(translations) != set(MESSAGE_IDS) or any(
            not value for value in translations.values()
        ):
            raise ValueError(f"{language} catalog is incomplete")
    result = {LOCALE_ROOT / f"{DOMAIN}.pot": _render_pot()}
    for language, translations in inventories.items():
        base = LOCALE_ROOT / language / "LC_MESSAGES" / DOMAIN
        result[base.with_suffix(".po")] = _render_po(language, translations)
        result[base.with_suffix(".mo")] = _compile_mo(language, translations)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    mismatches = []
    for path, expected in expected_files().items():
        if args.check:
            if not path.is_file() or path.read_bytes() != expected:
                mismatches.append(path.relative_to(ROOT).as_posix())
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(expected)
    if mismatches:
        raise SystemExit("locale files are stale: " + ", ".join(mismatches))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
