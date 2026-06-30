# 新人入群资料转发插件

适用环境：AstrBot + llbot/LLOneBot/OneBot v11，平台适配器为 `aiocqhttp`。

## 现在不需要手动找 message_id

推荐工作流：

1. 管理员私聊机器人：`开始`
2. 继续私聊发送要转发给新人的内容：
   - 普通文字
   - 图片
   - QQ 合并聊天记录
3. 管理员私聊机器人：`结束`
4. 有新人进群时，机器人自动私聊新人，把刚才保存的内容按顺序发过去。

跨群限次去重：

- 默认按 QQ 跨所有群统计发送次数，`max_deliveries_per_recipient` 默认是 `2`。
- 也就是说，同一个 QQ 不管进哪个群，成功收到 2 次新人资料后就不会继续重复发送。
- 要改成最多 1 次、3 次或不限次数，直接改后台配置；填 `0` 或负数表示不限次数。

私聊一图/两图回复：

- 用户私聊机器人发送 1 张图片后，插件会等待 `image_wait_seconds` 秒；窗口结束仍只有 1 张图时，发送一图回复。
- 等待窗口内收到第 2 张图片时，立即发送两图回复。
- 管理员正在“开始/结束”录制新人资料时，图片仍按新人资料录制，不会触发一图/两图回复。
- 后台图片字段是上传文件类型，不需要填写 URL；也可以通过下面的指令直接添加图片。

临时私聊被拒绝时：

- 插件会先按新人入群事件尝试私聊发送。
- 如果 llbot/QQ 拒绝群临时私聊，插件不会把这次算作已发送。
- 插件会把该新人加入挂起补发队列，并可在群里 @ 提醒新人先私聊机器人。
- 新人主动私聊机器人后，插件会用已经打开的私聊链路继续发送原始聊天记录卡片和其他资料。

真人式 QQ 桌面开路：

- 默认关闭，打开 `qq_human_group_warmup_enabled` 后才会在新人入群时执行。
- 执行顺序是：拉起/聚焦来源群窗口，识别来源群，找新人昵称或 QQ，点击资料卡里的“发消息/聊天”，发送 `forward_warmup_message_text`。
- 如果一开始没识别到来源群窗口，会先可见地聚焦 QQ，并尝试通过 QQ 搜索框查找来源群。
- PC QQ 提示“不支持本功能”时，不要开启协议拉群；真人开路默认不再使用 `mqqapi://` 拉群。
- 这一步只负责发送额外第一条开路文字；成功后仍继续走原来的原始聊天记录卡片转发链路，不拆聊天记录、不重建兜底。
- 双 QQ 登录时建议保持 `qq_human_group_warmup_require_group_hint` 和 `qq_human_group_warmup_require_target_hint` 开启，避免点错窗口。

也可以使用更明确的控制词：

- `新人欢迎开始`
- `新人欢迎结束`
- `新人欢迎取消`
- `新人欢迎状态`
- `新人欢迎清空`

## 命令

- `开始`：开始录制新人资料。
- `结束`：保存本次录制，并覆盖旧资料。
- `取消`：放弃本次录制，不覆盖旧资料。
- `状态`：查看是否正在录制、当前录了几条、已保存几条。
- `清空`：清空已保存资料。
- `/新人欢迎测试 [QQ号] [来源群号]`：把已保存资料私聊发送给指定 QQ；带来源群号时会按该群的临时会话路径测试；管理员发送、机器人自身消息上报、以及机器人通过 AstrBot 发出的同名文本都可以触发。
- `/新人欢迎真人开路测试 QQ号 来源群号`：强制执行一次真人式 QQ 桌面开路，用于远端测试，不需要等新人入群；失败时会返回 `stage/reason`。
- `/新人欢迎诊断 QQ号 来源群号`：检查 llbot 是否能在来源群成员列表里看到该 QQ，用来判断临时会话失败原因。
- `/添加一图回复图片`：私聊机器人后直接发送图片，保存为一图回复图片。
- `/添加两图回复图片`：私聊机器人后直接发送图片，保存为两图回复图片。
- `/删除一图回复图片`：删除通过指令添加的一图回复图片。
- `/删除两图回复图片`：删除通过指令添加的两图回复图片。

## 安装

把整个 `astrbot_plugin_new_member_forwarder` 文件夹复制到 AstrBot 的插件目录：

```text
AstrBot/data/plugins/astrbot_plugin_new_member_forwarder
```

然后在 AstrBot WebUI 插件管理里重载插件或重启 AstrBot。

## 配置

- `admin_user_ids`：管理员 QQ 号列表。建议填自己的 QQ 号；留空表示任何私聊用户都能录制。
- `allowed_groups`：生效群号列表，留空表示所有群。
- `message_gap_seconds`：新人资料有多条私信时，每条私信之间的间隔秒数。
- `delivery_limit_enabled`：是否启用发送次数限制。
- `delivery_limit_scope`：`user` 表示不管什么群都按 QQ 统计；`user_group` 表示同一 QQ 在不同群分别统计。
- `max_deliveries_per_recipient`：同一个统计对象最多发送几次；默认 `2`，填 `0` 或负数表示不限次数。
- `delivery_history_expire_days`：发送次数记录保留天数；默认 `0` 表示永不过期。
- `prepare_temp_session_before_send`：发送前读取一次群成员信息，帮助 llbot 准备群临时会话。
- `validate_group_member_before_send`：发送前刷新群成员列表并确认目标 QQ 在来源群内；如果 llbot 当前没有认到该群员，会等待重试而不是盲发。
- `temp_session_retry_delays_seconds`：遇到“请先添加对方为好友”时的重试等待秒数列表；当前默认 `5,15,30,60`。
- `forward_warmup_message_enabled`：发送录制资料前额外发送第一条普通私聊开路消息；该消息不需要录制。
- `forward_warmup_message_text`：额外第一条开路消息内容，后台可直接修改；留空则不发送。
- `forward_warmup_delay_seconds`：开路消息发送成功后，等待多少秒再发送录制资料。
- `qq_human_group_warmup_enabled`：是否启用真人式 QQ 桌面开路。
- `qq_human_group_warmup_wait_seconds` / `qq_human_group_warmup_timeout_seconds`：真人式开路查找窗口和总超时。
- `qq_human_group_warmup_require_group_hint` / `qq_human_group_warmup_require_target_hint`：是否强制校验来源群和新人线索，双 QQ 登录建议开启。
- `qq_human_group_warmup_member_search_enabled`：找不到新人时是否尝试在群窗口搜索新人昵称或 QQ。
- `qq_human_group_warmup_group_search_enabled`：找不到来源群窗口时是否尝试聚焦 QQ 并搜索来源群。
- `qq_human_group_warmup_force_open_group_protocol_enabled`：是否强制使用 QQ 协议拉群；当前 PC QQ 弹“不支持本功能”时必须保持关闭。
- `pending_delivery_enabled`：入群时临时私聊被拒绝后，是否挂起发送，等新人先私聊机器人后自动补发资料。
- `pending_expire_seconds`：挂起补发记录保留秒数；默认 `86400`。
- `pending_delivery_notice_enabled`：挂起补发时是否在群里提醒新人先私聊机器人。
- `pending_delivery_notice_at_user`：群内挂起提醒是否 @ 新人。
- `pending_delivery_notice_text`：群内挂起提醒文字，支持 `{user_id}` 和 `{group_id}` 占位符。
- `save_incoming_images`：录制图片时尝试下载到本地，避免临时图片链接过期。
- `private_image_reply_enabled`：是否启用私聊一图/两图自动回复。
- `one_image_reply` / `two_image_reply`：一图/两图回复文字。
- `one_image_reply_image` / `two_image_reply_image`：后台直接上传的一图/两图回复图片。
- `one_image_reply_order` / `two_image_reply_order`：文字和图片的发送顺序。

录制后的数据存储在：

```text
AstrBot/data/plugin_data/astrbot_plugin_new_member_forwarder/recorded_material.json
AstrBot/data/plugin_data/astrbot_plugin_new_member_forwarder/image_reply_assets.json
AstrBot/data/plugin_data/astrbot_plugin_new_member_forwarder/delivery_history.json
AstrBot/data/plugin_data/astrbot_plugin_new_member_forwarder/pending_deliveries.json
AstrBot/data/plugin_data/astrbot_plugin_new_member_forwarder/media/
```

## 注意

- 新人入群后只会私聊新人，不会发群里。
- 私聊是否成功取决于 llbot/OneBot 端能力、QQ 风控、来源群是否正确，以及 llbot 是否已同步到该群成员；如果入群时被拒绝，插件会挂起，等新人主动私聊机器人后补发。
- QQ 合并聊天记录会保存原始 `forward_id` 并按原始聊天记录卡片私聊转发，不拆分、不重建。旧录制内容如果不是原始卡片，请重新“开始/结束”录制一次。
