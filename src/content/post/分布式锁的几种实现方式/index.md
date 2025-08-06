---
title: "分布式锁的几种实现方式"
description: "分布式锁的几种实现方式"
publishDate: "17 February 2020"
tags: ['zookeeper', 'etcd', '分布式', 'redis', 'golang']
---

分布式锁作为一种多机间的同步手段在分布式环境下应用广泛，这篇文章分别用redis，etcd，zookeeper讨论一下如何实现分布式锁。

<!-- more -->

## 接口设计

```go
type Lock interface{
	Lock() <-chan bool //true for get the lock,false for error
	UnLock()
}
```
Lock函数我设计成返回一个channel，其实etcd，zookeeper的sdk里都有分布式锁的实现，但是使用方法都是只能阻塞方式用，设计成channel可以支持非阻塞，方便用户自由使用。

## redis实现分布式锁

redis实现分布式锁的原理主要依靠redis执行命令的单线程操作，使用`SETNX`保证只有一个客户端可以将key设置成功，之后其他客户端监听key状态等待key被删除后尝试获得锁，在`SETNX`的官方文档下面给出了一个分布式锁的实现参考
```
SETNX lock.foo <current Unix time + lock timeout + 1>
```
使用`SETNX`获取锁，key是锁的名字，value是持有锁的时间戳，set成功获得锁，释放锁时del对应key，在持有锁时还应该对锁进行续期，如果没有set成功，则不断get，如果key不存在或者value已经过期，则`GETSET`设置新的时间戳，如果返回的时间戳未超时，说明有另一个客户端getset成功了，需要继续等待。之所以不能直接DEL之后SETNX是因为这里会有一个race condition：如果C1,C2两个客户端都get后发现key超时，C1执行DEL,SETNX获得锁，之后C2同样执行DEL,SETNX获得了锁，使用getset解决了这个问题。

这是文档给出的一种实现方法，这样实现有几个问题，一个是value设置的是时间戳，多机之间的时间可能有误差，还有如果续期更新超时时间失败，由于释放锁时直接DEL对应key，可能有其他客户端此时获得了锁，就会出现释放掉其他客户端获得的锁的问题，文档里还给出了一种单机的正确实现方法:

```
SET resource_name my_random_value NX PX 30000

if redis.call("get",KEYS[1]) == ARGV[1] then
    return redis.call("del",KEYS[1])
else
    return 0
end
```

value不再是时间戳，而是一个随机值，超时时间使用PX设置，释放锁时判断当前的value是否是之前设置的值，防止释放其他客户端获得的锁。

```go
//RedisLock impl DistributedLock
type RedisLock struct {
	redis   *redis.Pool
	name    string
	value   int64
	timeout int64
	stop    chan bool
	get     chan bool
}

//Lock get lock
func (l *RedisLock) Lock() <-chan bool {
	conn := l.redis.Get()
	defer conn.Close()
	var res int
	var err error
	l.value = rand.Int63()
	if res, err = redis.Int(conn.Do("SET", l.name, l.value, "NX", "PX", l.timeout)); err != nil {
		log.Println(res)
	}
	if res == 1 {
		go l.expire()
		l.get <- true
	} else if res == 0 {
		go l.check()
	}
	return l.get
}

//UnLock release lock
func (l *RedisLock) UnLock() {
	l.stop <- true
}

func (l *RedisLock) check() {
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case t := <-ticker.C:
			conn := l.redis.Get()
			defer conn.Close()
			var res int
			var err error
			if res, err = redis.Int(conn.Do("SET", l.name, l.value, "NX", "PX", l.timeout)); err != nil {
				log.Println(res)
			}
			if res == 1 {
				l.get <- true
				return
			}
			log.Println("Current time: ", t)
		case <-l.stop:
			return
		}
	}
}

func (l *RedisLock) expire() {
	ticker := time.NewTicker(time.Duration(l.timeout/2) * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-l.stop:
			log.Println("UnLock")
			conn := l.redis.Get()
			defer conn.Close()
			script := redis.NewScript(1, luaScript)
			if res, err := script.Do(conn, l.name, l.value); err != nil {
				log.Println(res)
			}
			l.redis.Close()
			return
		case t := <-ticker.C:
			conn := l.redis.Get()
			defer conn.Close()
			if res, err := conn.Do("PEXPIRE", l.name, l.timeout); err != nil {
				log.Println(res)
			}
			log.Println("Current time: ", t)
		}
	}
}
```
还有一种官方提出的分布式算法redlock，其实就是在多台redis上执行单机算法，一半以上节点成功视为获得锁，如果用了一些缓存中间件这个方法可能不太实用，而且算法的正确性有很多争议，有空再看一下。

## etcd实现分布式锁

etcd是分布式强一致性的kv存储，使用raft算法保证了多机的强一致性，在集群半数以上节点存活的情况下整个系统可用。etcdv3版本的API完全基于grpc，用etcd实现分布式锁主要使用lease这个特性，就是申请一个指定时间的租约，租约期间其它人无法操作，与redis实现的思想类似，去set同一个key的值，set成功视为获得锁，set不成功使用etcd的watch机制，可以监听key的变化，监听到删除操作后重试。申请lease后需要调用keepalive保持lease的有效状态。这里有一个需要注意的地方：根据sdk文档描述，keepalive返回的channel一定要及时消费，不然会频繁向etcd发送keepalive消息。每一次key操作都会有一个revisionid，相当于一个递增的序列号，watch的时候指定revisionid这样就不会丢事件。
大体的实现思路：申请lease->开启goroutine保持lease alive->set指定key->成功返回->失败开启goroutine,watch对应key的delete事件，重试。

```go
//EtcdLock impl DistributedLock
type EtcdLock struct {
	etcd    *clientv3.Client
	leaseID clientv3.LeaseID
	name    string
	timeout int64
	get     chan bool
	cancel  context.CancelFunc  //api支持context，就不需要用stop channel了
}

//Lock get lock
func (l *EtcdLock) Lock() <-chan bool {
	txn := clientv3.NewKV(l.etcd).Txn(context.TODO())
	lease := clientv3.NewLease(l.etcd)
	leaseResp, err := lease.Grant(context.TODO(), l.timeout)
	if err != nil {
		log.Println("lease grant:", err)
		l.get <- false
		return l.get
	}
	l.leaseID = leaseResp.ID
	ctx, cancel := context.WithCancel(context.TODO())
	l.cancel = cancel
	go l.leaseKeepAlive(ctx, l.leaseID, lease)

	txnResp, err := txn.If(clientv3.Compare(clientv3.CreateRevision(l.name), "=", 0)).
		Then(clientv3.OpPut(l.name, "test", clientv3.WithLease(l.leaseID))).
		Else().Commit()
	if err != nil {
		log.Println("txn:", err)
		l.get <- false
		return l.get
	}
	if !txnResp.Succeeded {
		log.Println("txnResp:", txnResp.Succeeded)
		go l.watchLock(ctx, txnResp.Header.GetRevision())
	} else {
		log.Println("lock success")
		l.get <- true
	}
	return l.get
}

//UnLock release lock
func (l *EtcdLock) UnLock() {
	log.Println("unlock")
	if l.cancel != nil {
		l.cancel()
	}
}

func (l *EtcdLock) leaseKeepAlive(ctx context.Context, leaseID clientv3.LeaseID, lease clientv3.Lease) {
	ch, err := lease.KeepAlive(ctx, leaseID)
	if err != nil {
		log.Println("lease keepalive:", err)
	} else {
		for res := range ch {
			log.Println("keepalive res:", res)
		}
	}
	//cancel后ch被关闭，释放lease
	if _, err := lease.Revoke(context.TODO(), leaseID); err != nil {
		log.Println("revoke err:", err)
	}
}

func (l *EtcdLock) watchLock(ctx context.Context, revision int64) {
	ch := l.etcd.Watch(ctx, l.name, clientv3.WithRev(revision + 1))
	for res := range ch {
		log.Println("watch res:", res)
		for _, ev := range res.Events {
			log.Println("events:", ev)
			if ev.Type == clientv3.EventTypeDelete {
				txn := clientv3.NewKV(l.etcd).Txn(context.TODO())
				txnResp, err := txn.If(clientv3.Compare(clientv3.CreateRevision(l.name), "=", 0)).
					Then(clientv3.OpPut(l.name, "test", clientv3.WithLease(l.leaseID))).
					Else().Commit()
				if err != nil {
					l.get <- false
					log.Println("txn:", err)
				}
				if txnResp.Succeeded {
					l.get <- true
					return
				} else {
					log.Println("txnResp:", txnResp.Succeeded)
				}
			}
		}
	}
}
```

## zookeeper实现分布式锁

zookeeper的功能和etcd类似，但是zookeeper更像是文件系统的操作方式，比如建一个/test/lock首先需要建立/test，而etcd把这些全抽象为kv，有相同前缀的key可以视为一个目录下，通过prefix得到一个目录下的所有kv值。使用zookeeper实现分布式锁的方式稍有不同，它有一个临时有序节点的概念，有序是多个客户端同时在一个目录下创建有序节点时会依次创建并返回序列号，临时是当客户端断开时自动删除节点。所以分布式锁的实现就是在同一个目录下创建临时有序节点，最小的节点视为获得锁，其它节点去watch比自己小一号的节点变化，按照节点顺序获得锁。
zookeeper的watch机制不像etcd统一了各种事件，有watch children，watch node，watch exist，watch children只能watch子节点的创建和删除，不能watch子节点值的变化，watch node只能对已经存在的node进行watch，不存在的node需要watch exist，watch返回的channel只有一个事件，读取后就关闭了，而且不像etcd有revisionid，在获取事件信息之后重新watch这段时间就可能丢事件。

这个的实现我基本就是把sdk里的Lock实现改成了返回channel的模式
```go
//ZkLock impl DistributedLock
type ZkLock struct {
	zk       *zk.Conn
	name     string
	lockpath string
	timeout  int64
	stop     chan bool
	get      chan bool
}

func parseSeq(path string) (int, error) {
	parts := strings.Split(path, "-")
	return strconv.Atoi(parts[len(parts)-1])
}

//Lock get lock
func (l *ZkLock) Lock() <-chan bool {
	lock := zk.NewLock(l.zk, "/lock", zk.AuthACL(zk.PermAll))
	lock.Lock()
	if l.lockpath != "" {
		l.get <- false
		return l.get
	}

	prefix := fmt.Sprintf("%s/lock-", l.name)

	path := ""
	var err error
	for i := 0; i < 3; i++ {
		path, err = l.zk.CreateProtectedEphemeralSequential(prefix, []byte{}, zk.AuthACL(zk.PermAll))
		if err == zk.ErrNoNode {
			// Create parent node.
			parts := strings.Split(l.name, "/")
			pth := ""
			for _, p := range parts[1:] {
				var exists bool
				pth += "/" + p
				exists, _, err = l.zk.Exists(pth)
				if err != nil {
					l.get <- false
					return l.get
				}
				if exists == true {
					continue
				}
				_, err = l.zk.Create(pth, []byte{}, 0, zk.AuthACL(zk.PermAll))
				if err != nil && err != zk.ErrNodeExists {
					l.get <- false
					return l.get
				}
			}
		} else if err == nil {
			break
		} else {
			l.get <- false
			return l.get
		}
	}
	if err != nil {
		l.get <- false
		return l.get
	}

	seq, err := parseSeq(path)
	if err != nil {
		l.get <- false
		return l.get
	}
	go l.watchLock(seq, path)
	return l.get
}

func (l *ZkLock) watchLock(seq int, path string) {
	for {
		children, _, err := l.zk.Children(l.name)
		if err != nil {
			l.get <- false
			return
		}

		lowestSeq := seq
		prevSeq := -1
		prevSeqPath := ""
		for _, p := range children {
			s, err := parseSeq(p)
			if err != nil {
				l.get <- false
			}
			if s < lowestSeq {
				lowestSeq = s
			}
			if s < seq && s > prevSeq {
				prevSeq = s
				prevSeqPath = p
			}
		}

		if seq == lowestSeq {
			// Acquired the lock
			l.get <- true
			l.lockpath = path
			break
		}

		// Wait on the node next in line for the lock
		_, _, ch, err := l.zk.GetW(l.name + "/" + prevSeqPath)
		if err != nil && err != zk.ErrNoNode {
			l.get <- false
			return
		} else if err != nil && err == zk.ErrNoNode {
			// try again
			continue
		}
		select {
		case ev := <-ch:
			if ev.Err != nil {
				l.get <- false
				return
			}
		case <-l.stop:
			return
		}
	}
}

//UnLock release lock
func (l *ZkLock) UnLock() {
	l.stop <- true
	if l.lockpath == "" {
		return
	}
	if err := l.zk.Delete(l.lockpath, -1); err != nil {
		log.Println("delete lock error:", err)
	}
}
```