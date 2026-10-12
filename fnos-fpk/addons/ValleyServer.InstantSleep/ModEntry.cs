using System;
using System.Linq;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewModdingAPI.Events;
using StardewValley;
using StardewValley.Locations;
using StardewValley.Objects;

namespace ValleyServer.InstantSleep;

/// <summary>
/// 任一名联机玩家上床后，主机玩家**立即**跟着入睡，让服务器马上结算当天。
///
/// 设计要点（都是真机踩过的坑）：
///   1. 农舍的位置名是 <c>"FarmHouse"</c>（大写 H）—— 写成 "Farmhouse" 时
///      <see cref="Game1.warpFarmer"/> 会**静默失败**，主机永远走不到床边，
///      于是只能反复调 startSleep（实测刷了 129 行日志、拖了 2.5 分钟）。
///   2. warp 要到**本 tick 末尾**才生效，所以必须"先 warp、下一轮再 startSleep"；
///      上游 ALOS 的 alos.go_to_sleep 就是栽在这里（warp 后立刻在室外地点反射调
///      startSleep → NotImplementedException）。
///   3. 必须做**冷却与有界重试**：早先版本每 0.5 秒重试一次、每次都打日志，
///      既刷屏又反复打断游戏的结算流程，反而让"当天结算"变慢。
/// </summary>
public class ModEntry : Mod
{
    /// <summary>农舍位置名（⚠️ 大小写必须与游戏一致：FarmHouse）。</summary>
    private const string FarmHouseName = "FarmHouse";

    private const int TickInterval = 15;        // 检查间隔（tick）≈ 0.25 秒
    private const int WarpCooldown = 120;       // 每次 warp 后等 ≈2 秒
    private const int SleepCooldown = 300;      // 每次 startSleep 后等 ≈5 秒
    private const int MaxWarps = 20;            // 一轮最多 warp 次数
    private const int MaxSleeps = 12;           // 一轮最多 startSleep 次数

    private int _ticks;
    private int _cooldown;
    private int _warps;
    private int _sleeps;
    private bool _episode;      // 本轮（有玩家已入睡）是否已打过"开始"日志
    private bool _loggedDiag;   // 本轮是否已打过诊断信息
    private bool _gaveUp;

    public override void Entry(IModHelper helper)
    {
        helper.Events.GameLoop.UpdateTicked += this.OnUpdateTicked;
    }

    private void OnUpdateTicked(object? sender, UpdateTickedEventArgs e)
    {
        if (!Context.IsWorldReady || !Context.IsMultiplayer)
            return;

        if (++this._ticks < TickInterval)
            return;
        this._ticks = 0;

        // 主机已在床上 → 本轮结束（接下来由游戏与 ALOS 完成结算）
        if (Game1.player.isInBed.Value)
        {
            this.EndEpisode();
            return;
        }

        if (!this.AnyGuestAsleep())
        {
            this.EndEpisode();
            return;
        }

        if (!this._episode)
        {
            this._episode = true;
            this._gaveUp = false;
            this._warps = 0;
            this._sleeps = 0;
            this._cooldown = 0;
            this._loggedDiag = false;
            this.Monitor.Log("检测到其他玩家已入睡 → 让主机立刻跟着入睡。", LogLevel.Info);
        }

        if (this._gaveUp)
            return;
        if (this._cooldown > 0)
        {
            this._cooldown--;
            return;
        }

        this.PutHostToBed();
    }

    private bool AnyGuestAsleep()
    {
        try
        {
            // Game1.otherFarmers 不含主机自己；isInBed 是同步字段（NetBool）
            return Game1.otherFarmers.Values.Any(f => f.isInBed.Value);
        }
        catch (Exception ex)
        {
            this.Monitor.Log($"读取玩家上床状态失败：{ex.GetType().Name}: {ex.Message}", LogLevel.Error);
            return false;
        }
    }

    private void PutHostToBed()
    {
        Point bedSpot = this.GetBedSpot();
        Vector2? bedTile = this.GetBedTile();

        if (!this._loggedDiag)
        {
            this._loggedDiag = true;
            this.Monitor.Log(
                $"诊断：位置={(Game1.currentLocation?.Name ?? "?")} " +
                $"玩家tile=({Game1.player.TilePoint.X},{Game1.player.TilePoint.Y}) " +
                $"床tile={(bedTile.HasValue ? $"({bedTile.Value.X},{bedTile.Value.Y})" : "?")} " +
                $"GetBedSpot=({bedSpot.X},{bedSpot.Y}) isInBed={Game1.player.isInBed.Value}",
                LogLevel.Info);
        }

        // 第一步：必须站到**床位**上（只判断"在农舍里"是不够的 ——
        // 实测 warp 到错误坐标后主机站在门口，startSleep 便毫无效果）
        bool inFarmHouse = Game1.currentLocation is FarmHouse;
        bool onBedTile = inFarmHouse
            && (int)Game1.player.TilePoint.X == bedSpot.X
            && (int)Game1.player.TilePoint.Y == bedSpot.Y;

        if (!onBedTile && !Game1.player.isInBed.Value)
        {
            if (this._warps >= MaxWarps)
            {
                this.Monitor.Log("多次回床未成功，停止本轮（ALOS 的 22:00 逻辑仍会兜底）。",
                                 LogLevel.Warn);
                this._gaveUp = true;
                return;
            }

            this._warps++;
            Game1.warpFarmer(FarmHouseName, bedSpot.X, bedSpot.Y, false);
            this.Monitor.Log($"其他玩家已入睡 → 主机走到床位（{bedSpot.X},{bedSpot.Y}）。", LogLevel.Info);
            this._cooldown = WarpCooldown;
            return;
        }

        // 第二步：已站在床位 → 调游戏自身的 startSleep（私有方法，用 SMAPI 反射）
        if (this._sleeps >= MaxSleeps)
        {
            this.Monitor.Log("多次尝试入睡未生效，停止本轮（避免反复打断结算流程）。", LogLevel.Warn);
            this._gaveUp = true;
            return;
        }

        IReflectedMethod? sleep = this.Helper.Reflection
            .GetMethod(Game1.currentLocation, "startSleep", required: false);
        if (sleep is null)
        {
            this.Monitor.Log("找不到 FarmHouse.startSleep：游戏版本可能已变化，无法让主机入睡。",
                             LogLevel.Warn);
            this._gaveUp = true;
            return;
        }

        this._sleeps++;
        try
        {
            sleep.Invoke();
            this.Monitor.Log($"其他玩家已入睡 → 主机入睡（startSleep），isInBed={Game1.player.isInBed.Value}。",
                             LogLevel.Info);
        }
        catch (Exception ex)
        {
            this.Monitor.Log($"startSleep 调用失败：{ex.GetType().Name}: {ex.Message}", LogLevel.Warn);
        }
        this._cooldown = SleepCooldown;
    }

    /// <summary>床家具自身的 tile（用于诊断与对齐）。</summary>
    private Vector2? GetBedTile()
    {
        try
        {
            if (Game1.getLocationFromName(FarmHouseName) is FarmHouse house)
            {
                BedFurniture? bed = house.furniture.OfType<BedFurniture>().FirstOrDefault();
                if (bed is not null)
                    return bed.TileLocation;
            }
        }
        catch
        {
            // 忽略：仅诊断用
        }
        return null;
    }

    /// <summary>本轮结束：重置状态，等下一次"有玩家上床"再触发。</summary>
    private void EndEpisode()
    {
        if (!this._episode)
            return;
        this._episode = false;
        this._gaveUp = false;
        this._warps = 0;
        this._sleeps = 0;
        this._cooldown = 0;
        this._loggedDiag = false;
    }

    /// <summary>取农舍床的位置：优先用游戏自身接口，取不到退回默认床坐标。</summary>
    private Point GetBedSpot()
    {
        try
        {
            if (Game1.getLocationFromName(FarmHouseName) is FarmHouse house)
            {
                BedFurniture? bed = house.furniture.OfType<BedFurniture>().FirstOrDefault();
                if (bed is not null)
                    return bed.GetBedSpot();
            }
        }
        catch (Exception ex)
        {
            this.Monitor.Log($"GetBedSpot() 调用失败：{ex.Message}", LogLevel.Trace);
        }
        return new Point(9, 9);   // 上游 ALOS 的默认床坐标
    }
}
