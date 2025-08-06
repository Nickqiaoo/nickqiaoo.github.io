---
title: "微服务模式下如何写业务逻辑？"
description: "微服务模式下如何写业务逻辑？"
publishDate: "15 January 2020"
tags: ['微服务', '系统设计']
---

如今微服务模式大行其道，那么服务如何划分，服务之间到底是如何交互的，在微服务模式下开发一个业务模块是怎么实现的？以B站“开源”的代码为例，我挑了一个比较简单的业务：历史记录，分析一下微服务下的业务逻辑该怎么写。

<!-- more -->

B站整个的业务代码仓库分为interface，service，job这三个目录，interface是直接对外暴露的接口，service是供服务之间调用的接口，job是异步任务逻辑，消费kafka。传统的web开发一般都是分为controller，service，dao这三层，controller负责路由，service实现业务逻辑，dao封装数据库操作。

## Interface
```go
group := e.Group("/x/v2/history", authSvc.User)
{
	group.POST("/add", addHistory)
}
func addHistory(c *bm.Context) {
    //参数校验...
    h = &model.History{
            Aid:  v.Aid,
            //...
        }
    h.ConvertType()
    c.JSON(nil, hisSvc.AddHistory(c, mid, 0, h))
}
```
以播放历史为例，入口在addHistory这个函数里，做参数绑定后对参数校验，创建实体类，调用service层的处理函数：
```go
func (s *Service) AddHistory(c context.Context, mid, rtime int64, h *model.History) (err error) {
	
	if h.TP == model.TypeBangumi || h.TP == model.TypeMovie || h.TP == model.TypePGC {
		msg := playPro{
			Type:     h.TP,
			//...
		}
		s.addPlayPro(&msg)
	}
	return s.addHistory(c, mid, h)
}

func (s *Service) addHistory(c context.Context, mid int64, h *model.History) (err error) {
	var cmd int64
	if h.TP < model.TypeArticle {
		s.historyDao.PushFirstQueue(c, mid, h.Aid, h.Unix)
	}
	if cmd, err = s.Shadow(c, mid); err != nil {
		return
	}
	if cmd == model.ShadowOn {
		return
	}
	h.Mid = mid
	s.serviceAdd(h)
	s.historyDao.AddCache(c, mid, h);
	s.addMerge(mid, h.Unix)
	return
}
```
可以看到主要逻辑都在这里，首先判断了稿件类型，如果是视频会记录播放进度，这里调用了addPlayPro，把记录发送到kafka生产者协程，做一个批量生产。但是在代码中没找到消费这个topic的代码。。。我觉得播放进度直接存在model.History里就好了，这个可能是兼容旧代码的接口吧。
```go
func (s *Service) addProPub(p *model.History) {
	select {
	case s.proChan <- p:
	default:
		log.Warn("s.proChan chan is full")
	}
}
func (s *Service) playProproc() {
	for {
		select {
		case msg = <-s.msgs:
			if msg == nil {
				if len(ms) > 0 {
					s.pushPlayPro(ms)
				}
				return
			}
			ms = append(ms, msg)
			if len(ms) < 100 {
				continue
			}
		case <-ticker.C:
		}
		if len(ms) == 0 {
			continue
		}
		s.pushPlayPro(ms)
		ms = make([]*playPro, 0, 100)
	}
}

func (s *Service) pushPlayPro(ms []*playPro) {
	key := fmt.Sprintf("%d%d", ms[0].Mid, ms[0].Sid)
	for j := 0; j < 3; j++ {
		if err := s.historyDao.PlayPro(context.Background(), key, ms); err == nil {
			return
		}
	}
}
```
之后的逻辑我就不放代码了，`PushFirstQueue`判断是否是当天看的第一个视频，如果是需要加经验，向kafka发送一个加经验的异步任务。对于当天的记录是用日期加mid%1000打到1000个set，value为mid，存在redis里，用ismember去判断。

`Shadow`判断用户是否关闭了记录开关，这里我看代码中有一个配置开关可以选择是否用rpc请求Service，如果没有配置或者RPC返回错误就去查询redis，redis没有的话查hbase。开关记录在redis里是用一个hash去存的，用mid/bucket打到各个bucket里，field是mid%bucket，value是开关设置。

`serviceAdd`是用一个带缓冲的异步任务协程去调用的Service的RPC新增播放历史的接口。

`AddCache`记录历史到redis，用zset保存记录，key是mid，score是观看时间，member是稿件id，详细信息用hash记录，key是mid，field是稿件id，value是详细信息。最后向kafka发送一个merge的异步任务，和palyPro同理是批量发送。

总结一下

## Service

Interface层通过RPC调用了Service层增加历史的接口
```go
func (s *Service) AddHistory(c context.Context, arg *pb.AddHistoryReq) (reply *pb.AddHistoryReply, err error) {
	if err = s.checkBusiness(arg.Business); err != nil {
		return
	}
	reply = &pb.AddHistoryReply{}
	// 用户忽略播放历史
	userReply, _ := s.UserHide(c, &pb.UserHideReq{Mid: arg.Mid})
	if userReply != nil && userReply.Hide {
		return
	}
	if err = s.dao.AddHistoryCache(c, arg); err != nil {
		return
	}
	s.addMerge(c, arg.Business, arg.Mid, arg.Kid, arg.ViewAt)
	return
}
```
`UserHide`函数查询记录开关设置，同样是先查redis，没有的话查tidb，然后缓存到redis中。
`AddHistoryCache`同Interface中的函数，记录历史到redis。
`addMerge`同样是向kafka发送一个merge的异步任务。

## Job

现在Job有两个异步任务，分别是Interface和Service的两个merge任务。

两个任务稍有不同，Interface写入kafka的信息是mid->time，所以flush的时候需要先从redis中的zset获得aid，然后hget获得详细信息后持久化，而Service写入kafka的是aid，可以直接hget。

对于Interface发来的merge，使用一个协程消费kafka，然后分发到多个merge协程，merge协程接收到一定数量消息后对mid只保留最新的刷新时间，调用interface的flush HTTP接口，flush接口就是根据刷新时间从zset获取aid，再hget获得信息后写入hbase，最后清除部分缓存。

Service的merge同理，只不过由于kafka中的消息是aid，所以是对相同的aid保留最新的刷新时间，从redis中直接hget获得信息后写入tidb，根据设置的limit清除部分缓存。

## 总结

总结一下，其实可以看到整个业务有两套逻辑，不知道是做冗余还是历史遗留问题：

一个是interface层写redis，然后job来merge，最后interface落地到hbase

一个是service层写redis，然后job来merge，最后service落地到tidb

至于为什么要用kafka异步任务的方式来写db我觉得应该是一个削峰的策略，通过控制消费速率保证db的一个平滑写入。

可以看到微服务模式下业务逻辑的开发比较方便，周边的各种生态提供好了了众多的基础功能，写起来很舒服。